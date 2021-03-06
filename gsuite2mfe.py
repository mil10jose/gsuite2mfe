
from apiclient import discovery
from configparser import NoOptionError
from configparser import SafeConfigParser
from datetime import datetime
from datetime import timedelta
from oauth2client import client
from oauth2client import tools
from oauth2client.file import Storage
from pyrfc3339 import parse
from pyrfc3339 import generate
from pytz import timezone

import argparse
import base64
import httplib2
import inspect
import ipaddress
import json
import logging
import logging.config
import pickle
import os
import pytz
import socket
import sys
import time


""" G Suite/Google Apps Events -> McAfee ESM

This will pull events from G Suite (formerly Google Apps) and forward the
events to a McAfee ESM.

The script requires Python 3 and was tested with 3.5.2 for Windows and Linux.

The module requirements list is quite extensive. Please see the requirements file.

The script requires a config.ini file for the Receiver IP and port. An alternate
config file can be specified from the command line.

An example config.ini is available at:
https://raw.githubusercontent.com/andywalden/gsuite2ESM/config.ini

This is intended to be called as a cron task. A bookmark file is created in the
same directory. If the bookmark file does not exist, one will be created and
future events will be forwarded.

Make sure the permissions on the config.ini file are secure as not to expose 
any credentials.

"""

__author__ = "Andy Walden"
__version__ = "Beta2"

class Args(object):
    """
    Handles any args and passes them back as a dict
    """

    def __init__(self, args):
        self.log_levels = ["quiet", "error", "warning", "info", "debug"]
        self.formatter_class = argparse.RawDescriptionHelpFormatter
        self.parser = argparse.ArgumentParser(
                formatter_class=self.formatter_class,
                description="Send Google GSuite events to McAfee ESM"
            )
        self.args = args

        self.parser.add_argument("-s", "--start",
                                 default=None, dest="s_time", metavar='',
                                 help="Set start time to retrieve events. Format: 2016-11-19T14:53:38.000Z")

        self.parser.add_argument("-e", "--end",
                                 default=None, dest="e_time", metavar='',
                                 help="Set end time to retrieve events. Format: 2016-11-19T14:53:38.000Z")

        self.parser.add_argument("-t", "--test",
                                 action='store_true',
                                 default=False,
                                 dest="testmode",
                                 help="Disable syslog forwarding. Combine with -l debug for console output")

        self.parser.add_argument("-w", "--write",
                                 action='store_true',
                                 default=False,
                                 dest="write_eventfile",
                                 help="Write events to log: gsuite2mfe_events.log. Use -f to change path/filename")
                
        self.parser.add_argument("-f", "--file",
                                 default=None, dest="event_filename", metavar='',
                                 help="Specify alternate path/filename for -w option")

        self.parser.add_argument("-v", "--version",
                                 action="version",
                                 help="Show version",
                                 version="%(prog)s {}".format(__version__))

        self.parser.add_argument("-l", "--level",
                                 default=None, dest="level",
                                 choices=self.log_levels, metavar='',
                                 help="Logging output level. Default: warning")

        self.parser.add_argument("-c", "--config",
                                 default=None, dest="cfgfile", metavar='',
                                 help="Path to config file. Default: config.ini")

        self.pargs = self.parser.parse_args()

    def get_args(self):
        return self.pargs


class Config(object):
    """ Creates object for provided configfile/section settings """

    def __init__(self, filename, header):
        config = SafeConfigParser()
        cfgfile = config.read(filename)
        if not cfgfile:
            raise ValueError('Config file not found:', filename)
        self.__dict__.update(config.items(header))


def logging_init():
    filename = get_filename()
    logfile = filename + ".log"
    hostname = socket.gethostname()
    formatter = logging.Formatter('%(asctime)s {} %(module)s: %(message)s'.format(hostname),
                                    datefmt='%b %d %H:%M:%S')
    logger = logging.getLogger()
    fh = logging.FileHandler(logfile, mode='a')
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    ch = logging.StreamHandler()
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logging.getLogger('googleapiclient').setLevel(logging.CRITICAL)

def get_filename():
    filename = (inspect.getfile(inspect.currentframe())
                .split("\\", -1)[-1]).rsplit(".", 1)[0]
    return filename
        
class Syslog(object):
    """
    Open TCP socket using supplied server IP and port.
    
    Returns socket or None on failure
    """

    def __init__(self,
                server,
                port=514):
        logging.debug("Function: open_socket: %s: %s", server, port)
        self.server = server
        self.port = int(port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.connect((self.server, self.port))
        except socket.timeout:
            logging.error("Connection timeout to syslog: %s", self.server)
        except socket.error:
            logging.error("Socket error to syslog: %s", self.server)
            
    def send(self, data):
        """
        Sends data to the established connection
        """
        self.data = data
        try:
            self.sock.sendall(data.encode())
            logging.info("Syslog feedback sent")
        except socket.timeout:
            logging.error("Connection timeout to syslog: %s", self.server)
        except socket.error:
            logging.error("Socket error to syslog: %s", self.server)


class GsuiteQuery(object):

    def __init__(self, app, s_time=None, e_time=None, max="50", user='all'):
        self.app = app
        self.s_time = s_time
        self.e_time = e_time
        self.max = max
        self.user = user
        self.scope = 'https://www.googleapis.com/auth/admin.reports.audit.readonly'
        self.secret_file = 'client_secret.json'
        self.appname = 'Reports API Python Quickstart'

    def get_credentials(self):
        """
        Returns user credentials from storage.

        If nothing has been stored, or if the stored credentials are invalid,
        the OAuth2 flow is completed to obtain the new credentials.
        """
        self.home_dir = os.path.expanduser('~')
        self.credential_dir = os.path.join(self.home_dir, '.credentials')
        if not os.path.exists(self.credential_dir):
            logging.debug("Cred directory not found...creating: %s", self.credential_dir)
            os.makedirs(self.credential_dir)
        self.credential_path = os.path.join(self.credential_dir,
                                       'admin-reports_v1-python-quickstart.json')

        self.store = Storage(self.credential_path)
        self.credentials = self.store.get()
        if not self.credentials or self.credentials.invalid:
            if not os.path.isfile(self.client_file):
                logging.error("'client_secret.json file is missing. \
                                 Google OAuth must be configured")
                sys.exit(1)

            self.flow = client.flow_from_clientsecrets(self.client_file, self.scope)
            self.flow.user_agent = self.appname
        
    def execute(self):
        """ 
        Returns GSuite events based on given app/activity.
        Other parameters are optional.
        """
        logging.debug("Authenticating to GSuite")
        self.get_credentials()
        self.http = self.credentials.authorize(httplib2.Http())
        self.service = discovery.build('admin', 'reports_v1', http=self.http)
        logging.debug("Retrieving %s events from: %s to %s", self.app, convert_time(self.s_time), convert_time(self.e_time))
        self.results = self.service.activities().list(userKey=self.user, 
                                             applicationName=self.app, 
                                             startTime=self.s_time,
                                             endTime=self.e_time,
                                             maxResults=self.max).execute()
        return self.results.get('items', [])
        
    
class Bookmark(object):
    """
    Functions to read, write, track update bookmark files
    """
    def __init__(self, activity):
        logging.debug("Init bookmark object: %s.", activity)
        self.activity = activity
        self.bmfile = "." + activity + '.bookmark'
    
    def read(self):
        """ 
        Returns RFC 3339 timestamp string. Tries to read given file.
        If file cannot be read, current time is returned.
        """
        logging.debug("Looking for bookmark file")
        try:
            if os.path.getsize(self.bmfile) < 10:
                logging.error("Bookmark file appears corrupt: %s", self.bmfile)
                self._generate_bm_time()
                return self.s_time

        except FileNotFoundError:
            logging.debug("Bookmark file not found: %s.", self.bmfile)
            self._generate_bm_time()
            return self.s_time

        try:
            with open(self.bmfile, 'r') as self.open_bmfile:
                logging.debug("Opening: %s", self.bmfile)
                self.bookmark = self.open_bmfile.read()
                logging.debug("File found. Reading timestamp: %s", 
                                convert_time(self.bookmark))
                if validate_time('s', self.bookmark):
                    logging.debug("Bookmark time is valid")
                    self.s_time = self.bookmark
                    return self.s_time
                else:
                    logging.error("Invalid bookmark data. Using current time")
                    self._generate_bm_time()
                    return self.s_time
        except OSError:
            logging.debug("Bookmark file cannot be accessed: %s.", self.bmfile)
            self._generate_bm_time()
            return self.s_time

    def _generate_bm_time(self):
        self.s_time = str(generate(datetime.now(pytz.utc) - timedelta(0,1800)))
        self.new_bookmark = validate_time('o', self.s_time)
        logging.debug("Bookmark time generated: %s", self.s_time)
        
    def update(self, events):
        """ 
        Stores latest timestamp as bookmark time.
        
        Validates RFC3339 timestamps for given list of events (record per line as dict). 
        
        """
        self.events = events
        for self.event in self.events:            
            self.evt_time_obj = validate_time('o', self.event['id']['time'])
            if self.evt_time_obj:
                if self.evt_time_obj > validate_time('o', self.bookmark):
                    self.new_bookmark = self.evt_time_obj
                    logging.debug("Event time > Bookmark time: %s", self.event['id']['time'])
                else:
                    logging.debug("Bookmark time > Event time. \
                                   Have latest event time: %s", self.event['id']['time'])
            else: 
                logging.error("Invalid event time. \
                               This should not happen: %s", self.event['id']['time'])

    def write(self):
        """ 
        Writes time to bookmark file. Adds one second to event.
        """

        try:
            self.new_bookmark_p1 = self.new_bookmark + timedelta(0,1)
            self.new_bookmark_str = generate(self.new_bookmark_p1)
            try:
                with open(self.bmfile, 'w') as self.open_bmfile:
                    self.open_bmfile.write(self.new_bookmark_str)
                    self.open_bmfile.flush()
                    logging.debug("Updated bookmark file: %s", self.new_bookmark_str)
            except OSError:
                    logging.error("Bookmark file could not be written")
        except AttributeError:
            logging.debug("No new timestamps. Bookmark remains unchanged")

class Cache(object):
    """
    Functions to create, read, write the event-id cache file
    """
    def __init__(self, activity):
        logging.debug("Building cache for: %s", activity)
        self.activity = activity
        self.cachefile = "." + activity + '.cache'
        self.cache_enabled = True
        self.cache = {}
        self._init_cache

    def _init_cache(self):
        """ 
        Try to open existing cache file, if no file, call _build_cache
        """
        logging.debug("Looking for cache file: %s", self.cachefile)
        if os.path.exists(self.cachefile) and os.path.getsize(self.cachefile) > 0:
            with open(self.cachefile, "rb") as self.open_cache:
                self.cache = pickle.load(self.open_cache)
                logging.debug("Cache: %s", (self.cache))
        else:
            logging.debug("Cache file not found. Creating from scratch")
            self._build_cache()
        
    def _build_cache(self):
        """ 
        Query G Suite to build event_id cache for given activity
        """
        self.gsuite = GsuiteQuery(self.activity, max="50")
        self.events = self.gsuite.execute()
        if len(self.events) > 0:
            for self.cnt, self.event in enumerate(self.events, 1):
                self.cache.update({self.event['id']['uniqueQualifier']:self.event['id']['time']})
            logging.debug("Cache built: New event IDs added: %s", self.cnt)
        else:
            self.cache_enabled = False
            logging.debug("No events found for cache. Caching disabled")
    
    def dedup_events(self, new_events):
        """
        Returns list with any cached events removed.
        
        Compares given list of events (record per line as a dict) with the cache 
        to look for duplicate events. 
        """
        logging.debug("Call to deduplicate events. Processing: %s", len(new_events))
        if self.cache_enabled:
            self.new_events = new_events
            self.deduped_events = []
            
            for self.new_event in self.new_events:
                self.new_event_id = self.new_event['id']['uniqueQualifier']
                self.new_event_time = self.new_event['id']['time']
                
                if self.new_event_id not in self.cache:
                    self.deduped_events.append(self.new_event)
                    self.cache.update({self.new_event_id : self.new_event_time})
                else:
                    logging.debug("Duplicate event found in cache: %s", self.new_event)
            return(self.deduped_events)
        else:
            logging.error("Caching disabled. No skipping dedup for %s", self.activity)
        
    def write(self):
        """
        Write cache to file
        """
        try:
            with open(self.cachefile, 'wb') as self.open_cache:
                pickle.dump(self.cache, self.open_cache)
                logging.debug("Cache file entries written: filename:cnt: %s:%s", 
                                self.cachefile, len(self.cachefile))
        except OSError:
            logging.error("Cache file could not be written: %s", self.cachefile)
        else:
            logging.error("Caching disabled. Touching file: %s", self.cachefile)
            touch(self.cachefile)

def validate_time(return_type, timestamp):
    """If the timestamp is valid a RFC 3339 formatted timestamp, a string
    or object will be returned based upon the return_type of either 's', or 'o'
    """
    try:
        logging.debug("Validating timestamp: %s", convert_time(timestamp))
        time_obj = parse(timestamp)
        if return_type == 'o':
            return time_obj
        else:
            return timestamp
    except (ValueError, TypeError):
        if timestamp is None:
            logging.debug("Null time provided: %s", timestamp)
        else:
            logging.error("Invalid time format: %s", timestamp)
        return None
                    
def convert_time(timestamp):    
    return str(parse(timestamp).astimezone(pytz.timezone("US/Eastern")))

            
def touch(touchfile, times=None):
    """
    touch - change file timestamps
    """
    with open(touchfile, 'a'):
        os.utime(touchfile, times)            
            
def send_to_syslog(events, syslog):
    """ 
    Sends iterable event object to syslog socket.
    """
    for cnt, event in enumerate(events, start=1):
        syslog.send(json.dumps(event))
        logging.debug("Event %s sent to syslog: %s.", cnt, json.dumps(event))
    logging.debug("Total Events: %s ", cnt)

def write_events_to_file(events, event_filename):
    """
    Writes list of events to a file.
    """
    try:
        with open(event_filename, 'a') as open_eventfile:
            for cnt, event in enumerate(events, start=1):
                json.dump(event, open_eventfile)
                open_eventfile.write('\n')
                logging.debug("Event %s written to file: %s.", cnt, json.dumps(event))
            open_eventfile.flush()
            logging.debug("Wrote events to file: %s", event_filename)
    except OSError:
        logging.error("Event file file could not be written: %s.", event_filename)
    except AttributeError:
        logging.debug("No new events. Event file unchanged")

        

def main():
    """ Main function """

    args = Args(sys.argv)
    pargs = args.get_args()
    logging_init()
    if pargs.level: 
        logging.getLogger().setLevel(getattr(logging, pargs.level.upper()))
    logging.debug("******************DEBUG ENABLED******************")
    testmode = pargs.testmode
    configfile = pargs.cfgfile if pargs.cfgfile else 'config.ini'

    try:
        event_filename = pargs.event_filename if pargs.event_filename else "gsuite2mfe_events.json"
    except NameError: 
        logging.debug("No event file specified")
    
    write_eventfile = True if pargs.write_eventfile else False
        
    try:
        c = Config(configfile, "DEFAULT")
        try:
            syslog_host = c.sysloghost
            syslog_port = c.syslogport
        except NoOptionError:
            logging.debug("'syslog_host' or 'syslog_port' setting \
                            not detected in: %s.", configfile)
            logging.debug("Enabling testmode")
            testmode = True
        try:
            activities = c.activities.split(',')
            logging.debug("Log retrieval enabled for: %s", activities)
        except AttributeError:
            activities = ['login']
            logging.error("'activities' setting not found in %s. \
                            Using 'login' as default.", configfile)
    except ValueError:
        logging.error("Config file not found: %s. Entering test mode.", configfile)
        testmode = True
        # Enabling login events for barebones testmode
        activities = ['login']

    using_bookmark = True

    s_time = pargs.s_time if validate_time('s', pargs.s_time) else None
    e_time = pargs.e_time if validate_time('s', pargs.e_time) else str(generate(datetime.now(pytz.utc)))
    if s_time:
        using_bookmark = False
         
    if not testmode:
        syslog = Syslog(syslog_host, syslog_port)
    
    for activity in activities:
        logging.debug("*****************")
        logging.debug("Processing actvity: '%s'", activity)
        logging.debug("*****************")
        if using_bookmark:
            bookmark = Bookmark(activity)
            s_time = bookmark.read()
            cache = Cache(activity)
            
        
        gsuite = GsuiteQuery(activity, s_time=s_time, e_time=e_time)
        events = gsuite.execute()
   
        if len(events) > 0 and using_bookmark:
            events = cache.dedup_events(events)
        
        if len(events) > 0:
            if using_bookmark:
                logging.debug("Validating event times")
                bookmark.update(events)
            if not testmode:
                send_to_syslog(events, syslog)
            if write_eventfile:
                write_events_to_file(events, event_filename)
            else:
                logging.debug("Total events retrieved from %s: %s", 
                                activity, len(events))
        else:
            logging.debug("No events found for activity: %s", activity)
        
        if using_bookmark:
            bookmark.write()
            cache.write()
        else:
            logging.debug("Bookmark unchanged")
            
if __name__ == "__main__":
    try:
        main()
        logging.debug("******************EXECUTE COMPLETE******************")
    except KeyboardInterrupt:
        logging.warning("Control-C Pressed, stopping..")
        sys.exit()
