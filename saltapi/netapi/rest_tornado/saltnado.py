'''

curl localhost:8888/login -d client=local -d username=username -d password=password -d eauth=pam

for testing
curl -H 'X-Auth-Token: 0cd364f0a3fa4f7d5c7bd07909031783' localhost:8888 -d client=local -d tgt='*' -d fun='test.ping'

# not working.... but in siege 3.0.1 and posts..
siege -c 1 -n 1 "http://127.0.0.1:8888 POST client=local&tgt=*&fun=test.ping"

# this works
ab -c 50 -n 100 -p body -T 'application/x-www-form-urlencoded' http://localhost:8888/

{"return": [{"perms": ["*.*"], "start": 1396137149.13565, "token": "0cd364f0a3fa4f7d5c7bd07909031783", "expire": 1396180349.135651, "user": "jacksontj", "eauth": "pam"}]}[jacksontj@Thomas-PC netapi]$ 


'''

import time

import sys

import tornado.httpserver
import tornado.ioloop
import tornado.web
import tornado.gen

from multiprocessing import Process

import math
import functools
import json
import yaml
import zmq
import msgpack
import fnmatch

# salt imports
import salt.utils
import salt.utils.event
import salt.client
import salt.runner
import salt.auth

# globals
context = zmq.Context()

'''
The clients rest_cherrypi supports. We want to mimic the interface, but not
    necessarily use the same API under the hood
# all of these require coordinating minion stuff
 - "local" (done)
 - "local_async" (done)
 - "local_batch" (done)

# master side
 - "runner" (done)
 - "wheel" (need async api...)

disbatch -> sends off the jobs and can set a handler and then call the main loop
'''


# TODO: refreshing clients using cachedict
saltclients = {'local': salt.client.LocalClient().run_job,
               # not the actual client we'll use.. but its what we'll use to get args
               'local_batch': salt.client.LocalClient.cmd_batch,
               'local_async': salt.client.LocalClient().run_job,
               'runner': salt.runner.RunnerClient(salt.config.master_config('/etc/salt/master')).async,
               }


AUTH_TOKEN_HEADER = 'X-Auth-Token'
AUTH_COOKIE_NAME = 'session_id'


def EventPublisher(mod_opts, opts):
    '''
    TODO: an API to change the subscribe thing (so we don't have to look at everything)
    Publish all events from the event bus to a zmq pub socket

    # TODO: some way to tell the tornado client that we are started up correctly
    '''
    pub_sock = context.socket(zmq.PUB)

    pub_sock.bind(mod_opts['pub_uri'])

    event = salt.utils.event.MasterEvent(opts['sock_dir'])

    for full_data in event.iter_events(tag='salt/', full=True):
        # TODO: different?
        tag = full_data['tag']
        data = full_data['data']
        if 'jid' not in data:
            continue
        if 'return' not in data:
            continue
        package = [data['jid'], msgpack.dumps(data)]
        pub_sock.send_multipart(package)

# TODO: move to a utils function within salt-- the batching stuff is a bit tied together
def get_batch_size(batch, num_minions):
    '''
    Return the batch size that you should have
    '''
    # figure out how many we can keep in flight
    partition = lambda x: float(x) / 100.0 * num_minions
    try:
        if '%' in batch:
            res = partition(float(batch.strip('%')))
            if res < 1:
                return int(math.ceil(res))
            else:
                return int(res)
        else:
            return int(batch)
    except ValueError:
        print(('Invalid batch data sent: {0}\nData must be in the form'
               'of %10, 10% or 3').format(batch))

class BaseSaltAPIHandler(tornado.web.RequestHandler):
    ct_out_map = (
        ('application/json', json.dumps),
        ('application/x-yaml', functools.partial(
            yaml.safe_dump, default_flow_style=False)),
    )

    def _verify_client(self, client):
        '''
        Verify that the client is in fact one we have
        '''
        if client not in saltclients:
            self.set_status(400)
            self.write('We don\'t serve your kind here')
            self.finish()

    @property
    def token(self):
        # find the token (cookie or headers)
        if AUTH_TOKEN_HEADER in self.request.headers:
            return self.request.headers[AUTH_TOKEN_HEADER]
        else:
            return self.get_cookie(AUTH_COOKIE_NAME)

    def _verify_auth(self):
        '''
        Boolean wether the request is auth'd
        '''

        return self.token and bool(self.application.auth.get_tok(self.token))

    def prepare(self):
        '''
        Run before get/posts etc. Pre-flight checks:
            - verify that we can speak back to them (compatible accept header)
        '''
        # verify the content type
        found = False
        for content_type, dumper in self.ct_out_map:
            if fnmatch.fnmatch(content_type, self.request.headers['Accept']):
                found = True
                break

        # better return message?
        if not found:
            self.send_error(406)

        self.content_type = content_type
        self.dumper = dumper
        
        # do the common parts
        self.start = time.time()
        self.connected = True
        
        self.lowstate = self._get_lowstate()

        # TODO: timeout per job? (since we have to wait for the longest one, 
        # this makes some sense
        timeout = 0
        for s in self.lowstate:
            s_timeout = s.get('timeout', self.application.opts['timeout'])
            if s_timeout > timeout:
                timeout = float(s_timeout)
        self.timeout = self.start + timeout

    def serialize(self, data):
        '''
        Serlialize the output based on the Accept header
        '''
        self.set_header('Content-Type', self.content_type)

        return self.dumper(data)

    def _form_loader(self, _):
        '''
        function to get the data from the urlencoded forms
        ignore the data passed in and just get the args from wherever they are
        '''
        data = {}
        for k, v in self.request.arguments.iteritems():
            if len(v) == 1:
                data[k] = v[0]
            else:
                data[k] = v
        return data

    def deserialize(self, data):
        '''
        Deserialize the data based on request content type headers
        '''
        ct_in_map = {
            'application/x-www-form-urlencoded': self._form_loader,
            'application/json': json.loads,
            'application/x-yaml': functools.partial(
                yaml.safe_load, default_flow_style=False),
            'text/yaml': functools.partial(
                yaml.safe_load, default_flow_style=False),
            # because people are terrible and dont mean what they say
            'text/plain': json.loads
        }

        try:
            if self.request.headers['Content-Type'] not in ct_in_map:
                self.send_error(406)
            return ct_in_map[self.request.headers['Content-Type']](data)
        except KeyError:
            return []

    def _get_lowstate(self):
        '''
        Format the incoming data into a lowstate object
        '''
        data = self.deserialize(self.request.body)

        if self.request.headers.get('Content-Type') == 'application/x-www-form-urlencoded':
            if 'arg' in data and not isinstance(data['arg'], list):
                data['arg'] = [data['arg']]
            print data
            lowstate = [data]
        else:
            lowstate = data
        return lowstate

    def _get_sub_sock(self, jids):
        '''
        Helper so we can call this from multiple places
        '''
        self.sub_sock = context.socket(zmq.SUB)
        for jid in jids:
            self.sub_sock.setsockopt(zmq.SUBSCRIBE, jid)
            # undo later?
            #self.sub_sock.setsockopt(zmq.UNSUBSCRIBE, jid)
        self.sub_sock.connect(self.application.mod_opts['pub_uri'])

    def _add_sub(self, jid):
        self.sub_sock.setsockopt(zmq.SUBSCRIBE, jid)

    def _rm_sub(self, jid):
        self.sub_sock.setsockopt(zmq.UNSUBSCRIBE, jid)

class SaltAuthHandler(BaseSaltAPIHandler):
    '''
    Handler for login resquests
    '''
    def get(self):
        '''
        We don't allow gets on the login path, so lets send back a nice message
        '''
        self.set_status(401)
        self.set_header('WWW-Authenticate', 'Session')

        ret = {'status': '401 Unauthorized',
               'return': 'Please log in'}

        self.write(self.serialize(ret))
        self.finish()

    # TODO: make async? Underlying library isn't... and we ARE making disk calls :(
    def post(self):
        '''
        Authenticate against Salt's eauth system
        {"return": {"start": 1395507384.320007, "token": "6ff4cd2b770ada48713afc629cd3178c", "expire": 1395550584.320007, "name": "jacksontj", "eauth": "pam"}}
        {"return": [{"perms": ["*.*"], "start": 1395507675.396021, "token": "dea8274dc359fee86357d9d0263ec93c0498888e", "expire": 1395550875.396021, "user": "jacksontj", "eauth": "pam"}]}
        '''
        creds = {'username': self.get_arguments('username')[0],
                 'password': self.get_arguments('password')[0],
                 'eauth': self.get_arguments('eauth')[0],
                 }

        token = self.application.auth.mk_token(creds)
        if not 'token' in token:
            # TODO: nicer error message
            # 'Could not authenticate using provided credentials')
            self.send_error(401)
            # return since we don't want to execute any more
            return

        # Grab eauth config for the current backend for the current user
        try:
            perms = self.application.opts['external_auth'][token['eauth']][token['name']]
        except (AttributeError, IndexError):
            logger.debug("Configuration for external_auth malformed for "
                         "eauth '{0}', and user '{1}'."
                         .format(token.get('eauth'), token.get('name')), exc_info=True)
            # TODO better error -- 'Configuration for external_auth could not be read.'
            self.send_error(500)

        ret = {'return': [{
            'token': token['token'],
            'expire': token['expire'],
            'start': token['start'],
            'user': token['name'],
            'eauth': token['eauth'],
            'perms': perms,
            }]}

        self.write(self.serialize(ret))
        self.finish()


class SaltAPIHandler(BaseSaltAPIHandler):
    def get(self):
        '''
        return data about what clients you have
        '''
        ret = {"clients": saltclients.keys(),
               "return": "Welcome"}
        self.write(self.serialize(ret))
        self.finish()

    @tornado.web.asynchronous
    def post(self):
        '''
        This function takes in all the args for dispatching requests
            **Example request**::

            % curl -si https://localhost:8000 \\
                    -H "Accept: application/x-yaml" \\
                    -H "X-Auth-Token: d40d1e1e" \\
                    -d client=local \\
                    -d tgt='*' \\
                    -d fun='test.ping' \\
                    -d arg

        '''
        # if you aren't authenticated, redirect to login
        if not self._verify_auth():
            self.redirect('/login')
            return

        client = self.get_arguments('client')[0]
        self._verify_client(client)
        self.disbatch(client)

    def disbatch(self, client):
        '''
        Disbatch a lowstate job to the appropriate client
        '''
        self.client = client
        
        for low in self.lowstate:
            if not ('token' in low or 'eauth' in low):
                # TODO: better error?
                self.set_status(401)
                self.finish()
                return        
        # disbatch to the correct handler
        try:
            getattr(self, '_disbatch_{0}'.format(self.client))()
        except AttributeError:
            # TODO set the right status... this means we didn't implement it...
            self.set_status(500)
            self.finish()

    def _current_inflight(self, batch_id):
        '''
        Helper to get the current number of minions in flight for a given batch
        '''
        count = 0
        for jid, batch_id in self.jid_map.iteritems():
            if batch_id == batch_id:
                count += 1
        return count

    def _disbatch_local_batch(self):
        '''
        Disbatch local client batched commands
        '''

        # dict of batch_id -> {'minions': minions_to_get, 'chunk': cunk}
        self.pub_map = {}
        # batch_id -> id -> return
        self.ret = {}

        # list of the jids that you have dispatched (in order)
        # map of jid -> batch_id
        self.jid_map = {}

        # dict of batch_id -> maxflight
        self.maxflight_map = {}

        # set our handler
        self.handler = self._handle_batch_minion_jobs

        self._get_sub_sock(jids=[])

        for batch_id, chunk in enumerate(self.lowstate):
            f_call = salt.utils.format_call(saltclients[self.client], chunk)
            batch_size = f_call['kwargs']['batch']
            # ping all the minions (to see who we have to talk to)
            # TODO: actually ping them all? this just gets the pub data
            minions = saltclients['local'](chunk['tgt'],
                                           'test.ping',
                                           [],
                                           expr_form=f_call['kwargs']['expr_form'])['minions']

            self.pub_map[batch_id] = {'minions': minions, 'chunk': chunk}
            self.maxflight_map[batch_id] = get_batch_size(f_call['kwargs']['batch'], len(minions))

            # start the batches!
            self._disbatch_batch_jobs(batch_id)

        
        tornado.ioloop.IOLoop.instance().add_callback(self.nonblocking_wait_loop)

    def _disbatch_batch_jobs(self, batch_id):
        '''
        Helper function to handle disbatching local_batch jobs
        '''
        f_call = salt.utils.format_call(saltclients['local'], self.pub_map[batch_id]['chunk'])
        while self._current_inflight(batch_id) < self.maxflight_map[batch_id]:
            f_call['args'][0] = self.pub_map[batch_id]['minions'].pop(0)
            # TODO: list??
            f_call['kwargs']['expr_form'] = 'glob'

            # fire a job off
            pub_data = saltclients['local'](*f_call.get('args', ()), **f_call.get('kwargs', {}))
            self.jid_map[pub_data['jid']] = batch_id
            self._add_sub(pub_data['jid'])

    def _disbatch_local(self):
        '''
        Disbatch local client commands
        '''
        # dict of jid -> minions_to_get
        self.pub_map = {}
        # jid -> id -> return
        self.ret = {}
        # list of the jids that you have dispatched (in order)
        self.jids = []

        # set our handler
        self.handler = self._handle_minion_jobs

        for chunk in self.lowstate:
            # TODO: not sure why.... we already verify auth, probably for ACLs
            # require token or eauth
            chunk['token'] = self.token

            f_call = salt.utils.format_call(saltclients[self.client], chunk)
            # fire a job off
            pub_data = saltclients[self.client](*f_call.get('args', ()), **f_call.get('kwargs', {}))
            self.pub_map[pub_data['jid']] = pub_data['minions']
            self.jids.append(pub_data['jid'])

        # TODO: buffer? This is a pretty obvious race condition :/
        # We are currently relying on the zmq pub buffer (HWM) to catch us
        self._get_sub_sock(jids=self.jids)

        tornado.ioloop.IOLoop.instance().add_callback(self.nonblocking_wait_loop)

    def _disbatch_local_async(self):
        '''
        Disbatch local client_async commands
        '''
        ret = []
        for chunk in self.lowstate:
            f_call = salt.utils.format_call(saltclients[self.client], chunk)
            # fire a job off
            pub_data = saltclients[self.client](*f_call.get('args', ()), **f_call.get('kwargs', {}))
            ret.append(pub_data)

        self.write(self.serialize({'return': ret}))
        self.finish()

    def _disbatch_runner(self):
        '''
        Disbatch runner client commands
        '''
        # jid -> id -> return
        self.ret = {}
        # list of the jids that you have dispatched (in order)
        self.jids = []

        # set our handler
        self.handler = self._handle_master_job

        for chunk in self.lowstate:
            f_call = {'args': [chunk['fun'], chunk]}
            pub_data = saltclients[self.client](chunk['fun'], chunk)
            # TODO: add the jid to the runner return dict
            # do some aerobics to get the jid...
            jid = pub_data['tag'].rsplit('/', 1)[-1]
            self.jids.append(jid)

        # TODO: buffer? This is a pretty obvious race condition :/
        # We are currently relying on the zmq pub buffer (HWM) to catch us
        self._get_sub_sock(jids=self.jids)

        tornado.ioloop.IOLoop.instance().add_callback(self.nonblocking_wait_loop)

    def _jobs_done(self):
        '''
        Are our inflight jobs done? (have some data for each)
        '''
        return self.ret.keys() == self.jids

    def nonblocking_wait_loop(self):
        '''
        Terrible name.. but this is the parent loop
        '''
        # TODO: log
        # if the client has disconnected, stop trying ;)
        if not self.connected:
            self.finish()
            return
        # if you are over timeout
        elif time.time() > self.timeout:
            print ('Enforce timeout')
            self.finish()
            # make sure to return, so we don't accidentally call again
            return

        try:
            jid, data = self.sub_sock.recv_multipart(zmq.NOBLOCK)
            data = msgpack.loads(data)
            # pass the data off to the handler
            self.handler(jid, data)

        except zmq.ZMQError as e:
            # if you got an EAGAIN, just schedule yourself for later
            if e.errno == zmq.EAGAIN:
                tornado.ioloop.IOLoop.instance().add_callback(self.nonblocking_wait_loop)
            # not sure what other errors we could get... but probably not good
            else:
                # TODO: 500 error or somesuch
                raise Exception()
        # catch other exceptions so we can log them?
        except:
            # TODO: log
            print sys.exc_info(), 'exception in main wait loop'
            self.finish()

    def _handle_master_job(self, jid, data):
        '''
        Loop to handle all master side jobs (runner, wheel, etc).
        This also means we are looking for a SINGLE return
        '''
        if jid not in self.jids:
            self.finish()
            return

        if jid not in self.ret:
            self.ret[jid] = {}
        self.ret[jid] = data['return']

        # TODO: stream this back? instead of the buffer and dump...
        if self._jobs_done():
            ret = []
            for jid in self.jids:
                ret.append(self.ret[jid])
            self.write(self.serialize({'return': ret}))
            self.finish()
        else:
            tornado.ioloop.IOLoop.instance().add_callback(self.nonblocking_wait_loop)

    def _handle_minion_jobs(self, jid, data):
        '''
        Handle jobs that require talking to minions
        '''
        if jid not in self.pub_map:
            self.finish()
            return

        # TODO: better error? Or just skip??
        if 'id' not in data:
            print 'Invalid minion return??'
            self.finish()
            return

        # remove this minion from the list of ones we are waiting on
        if data['id'] in self.pub_map[jid]:
            self.pub_map[jid].remove(data['id'])
        else:
            print 'Ummmm... ???'
            self.finish()
            return

        if jid not in self.ret:
            self.ret[jid] = {}
        self.ret[jid] = {data['id']: data['return']}

        if self._jobs_done():
            # TODO: make this nicer!
            ret = []
            for jid in self.jids:
                # jid -> id -> return
                ret.append(self.ret[jid])
            self.write(self.serialize({'return': ret}))
            self.finish()
        else:
            tornado.ioloop.IOLoop.instance().add_callback(self.nonblocking_wait_loop)

    def _handle_batch_minion_jobs(self, jid, data):
        '''
        Handle jobs that require talking to minions
        '''
        if jid not in self.jid_map:
            self.finish()
            return

        # TODO: better error? Or just skip??
        if 'id' not in data:
            print 'Invalid minion return??'
            self.finish()
            return

        batch_id = self.jid_map[jid]

        if batch_id not in self.ret:
            self.ret[batch_id] = {}

        self.ret[batch_id] = {data['id']: data['return']}

        # we have reaped the return, lets remove it (and remove the subscription)
        del self.jid_map[jid]
        self._rm_sub(jid)

        # if you have more minions, and you are now below the max, lets fire some jobs
        if len(self.pub_map[batch_id]['minions']) > 0 and self._current_inflight(batch_id) < self.maxflight_map[batch_id]:
            self._disbatch_batch_jobs(batch_id)

        if len(self.jid_map) == 0:
            # TODO: make this nicer!
            ret = []
            for batch_id in sorted(self.maxflight_map.keys()):
                # jid -> id -> return
                ret.append(self.ret[batch_id])
            self.write(self.serialize({'return': ret}))
            self.finish()
        else:
            tornado.ioloop.IOLoop.instance().add_callback(self.nonblocking_wait_loop)

    def _cleanup(self):
        '''
        Cleanup all stucts created for the specific request (to make sure we clean up zmq)
        '''
        if hasattr(self, 'sub_sock'):
            self.sub_sock.close()

    def on_finish(self):
        '''
        When the job has been done, lets cleanup
        '''
        self._cleanup()

    def on_connection_close(self):
        '''
        If the client disconnects, lets close out
        '''
        # TODO: log
        # TODO: another way to abort? this seems a bit messy
        print 'client closed connection'
        self.connected = False

class MinionSaltAPIHandler(SaltAPIHandler):
    '''
    Handler for /minion requests
    '''
    @tornado.web.asynchronous
    def get(self, mid):
        # if you aren't authenticated, redirect to login
        if not self._verify_auth():
            self.redirect('/login')
            return

        # overwrite the timeout, since we are making up our own lowstate
        self.timeout = self.start + self.application.opts['timeout']

        #'client': 'local', 'tgt': mid or '*', 'fun': 'grains.items',
        self.lowstate = [{
            'client': 'local', 'tgt': mid or '*', 'fun': 'grains.items',
        }]
        self.disbatch('local')

    @tornado.web.asynchronous
    def post(self):
        '''
        local_async post endpoint        
        '''
        # if you aren't authenticated, redirect to login
        if not self._verify_auth():
            self.redirect('/login')
            return

        self.disbatch('local_async')

class JobsSaltAPIHandler(SaltAPIHandler):
    '''
    Handler for /minion requests
    '''
    @tornado.web.asynchronous
    def get(self, jid=None):
        # if you aren't authenticated, redirect to login
        if not self._verify_auth():
            self.redirect('/login')
            return

        self.lowstate = [{
            'fun': 'jobs.lookup_jid' if jid else 'jobs.list_jobs',
            'jid': jid,
        }]

        if jid:
            self.lowstate.append({
                'fun': 'jobs.list_job',
                'jid': jid,
            })
        
        # overwrite the timeout, since we are making up our own lowstate
        self.timeout = self.start + self.application.opts['timeout'] + 100

        self.disbatch('runner')

class RunSaltAPIHandler(SaltAPIHandler):
    '''
    Handler for /run requests
    '''
    @tornado.web.asynchronous
    def post(self):
        client = self.get_arguments('client')[0]
        self._verify_client(client)
        self.disbatch(client)


