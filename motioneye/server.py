# Copyright (c) 2013 Calin Crisan
# This file is part of motionEye.
#
# motionEye is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import atexit
import datetime
import logging
import multiprocessing
import os
import re
import signal
import sys
import time

from tornado.ioloop import IOLoop
from tornado.web import Application

from motioneye import settings, template
from motioneye.controls import smbctl, v4l2ctl
from motioneye.handlers.action import ActionHandler
from motioneye.handlers.base import ManifestHandler, NotFoundHandler
from motioneye.handlers.config import ConfigHandler
from motioneye.handlers.log import LogHandler
from motioneye.handlers.login import LoginHandler
from motioneye.handlers.main import MainHandler
from motioneye.handlers.movie import MovieHandler
from motioneye.handlers.movie_playback import MovieDownloadHandler, MoviePlaybackHandler
from motioneye.handlers.picture import PictureHandler
from motioneye.handlers.power import PowerHandler
from motioneye.handlers.prefs import PrefsHandler
from motioneye.handlers.relay_event import RelayEventHandler
from motioneye.handlers.update import UpdateHandler
from motioneye.handlers.version import VersionHandler

_PID_FILE = 'motioneye.pid'
_CURRENT_PICTURE_REGEX = re.compile(r'^/picture/\d+/current')


class Daemon:
    def __init__(self, pid_file, run_callback=None):
        self.pid_file = pid_file
        self.run_callback = run_callback

    def daemonize(self):
        # first fork
        try:
            if os.fork() > 0:  # parent
                sys.exit(0)

        except OSError as e:
            sys.stderr.write('fork() failed: %s\n' % e.strerror)
            sys.exit(-1)

        # separate from parent
        os.setsid()
        os.umask(0)

        # second fork
        try:
            if os.fork() > 0:  # parent
                sys.exit(0)

        except OSError as e:
            sys.stderr.write('fork() failed: %s\n' % e.strerror)
            sys.exit(-1)

            # redirect standard file descriptors
        sys.stdout.flush()
        sys.stderr.flush()
        si = open('/dev/null')
        so = open('/dev/null', 'a+')
        se = open('/dev/null', 'a+')
        os.dup2(si.fileno(), sys.stdin.fileno())
        os.dup2(so.fileno(), sys.stdout.fileno())
        os.dup2(se.fileno(), sys.stderr.fileno())

        # pid file
        atexit.register(self.del_pid)
        with open(self.pid_file, 'w') as f:
            f.write('%s\n' % os.getpid())

    def del_pid(self):
        try:
            os.remove(self.pid_file)

        except:
            sys.stderr.write('failed to remove pid file.\n')

    def running(self):
        try:
            with open(self.pid_file) as f:
                pid = int(f.read().strip())

        except:
            return None

        try:
            os.kill(pid, 0)
            return pid

        except:
            return None

    def start(self):
        if self.running():
            sys.stderr.write('server is already running\n')
            sys.exit(-1)

        self.daemonize()
        sys.stdout.write('server started\n')
        self.run_callback()

    def stop(self):
        pid = self.running()
        if not pid:
            sys.stderr.write('server is not running\n')
            sys.exit(-1)

        try:
            os.kill(pid, signal.SIGTERM)

        except Exception as e:
            sys.stderr.write('failed to terminate server: %s\n' % e)

        for i in range(50):  # @UnusedVariable
            try:
                os.kill(pid, 0)
                time.sleep(0.1)

            except OSError as e:
                if str(e).count('No such process'):
                    self.del_pid()
                    sys.stdout.write('server stopped\n')
                    break

                else:
                    sys.stderr.write('failed to terminate server: %s\n' % e)
                    sys.exit(-11)

        else:
            sys.stderr.write('server failed to stop, killing it\n')
            try:
                os.kill(pid, signal.SIGKILL)

            except:
                sys.stderr.write('failed to kill...\n')


def _log_request(handler):
    log_method = None

    if handler.get_status() < 400:
        if not _CURRENT_PICTURE_REGEX.match(handler.request.uri):
            log_method = logging.debug

    elif handler.get_status() < 500:
        log_method = logging.warning

    else:
        log_method = logging.error

    if log_method:
        request_time = 1000.0 * handler.request.request_time()
        log_method(
            "%d %s %.2fms",
            handler.get_status(),
            handler._request_summary(),
            request_time,
        )


handler_mapping = [
    (r'^/$', MainHandler),
    (r'^/manifest.json$', ManifestHandler),
    (r'^/config/main/(?P<op>set|get)/?$', ConfigHandler),
    (
        r'^/config/(?P<camera_id>\d+)/(?P<op>get|set|rem|test|authorize)/?$',
        ConfigHandler,
    ),
    (r'^/config/(?P<op>add|list|backup|restore)/?$', ConfigHandler),
    (r'^/picture/(?P<camera_id>\d+)/(?P<op>current|list|frame)/?$', PictureHandler),
    (
        r'^/picture/(?P<camera_id>\d+)/(?P<op>download|preview|delete)/(?P<filename>.+?)/?$',
        PictureHandler,
    ),
    (
        r'^/picture/(?P<camera_id>\d+)/(?P<op>zipped|timelapse|delete_all)/(?P<group>.*?)/?$',
        PictureHandler,
    ),
    (r'^/movie/(?P<camera_id>\d+)/(?P<op>list)/?$', MovieHandler),
    (
        r'^/movie/(?P<camera_id>\d+)/(?P<op>preview|delete)/(?P<filename>.+?)/?$',
        MovieHandler,
    ),
    (r'^/movie/(?P<camera_id>\d+)/(?P<op>delete_all)/(?P<group>.*?)/?$', MovieHandler),
    (
        r'^/movie/(?P<camera_id>\d+)/playback/(?P<filename>.+?)/?$',
        MoviePlaybackHandler,
        {'path': r''},
    ),
    (
        r'^/movie/(?P<camera_id>\d+)/download/(?P<filename>.+?)/?$',
        MovieDownloadHandler,
        {'path': r''},
    ),
    (r'^/action/(?P<camera_id>\d+)/(?P<action>\w+)/?$', ActionHandler),
    (r'^/prefs/(?P<key>\w+)?/?$', PrefsHandler),
    (r'^/_relay_event/?$', RelayEventHandler),
    (r'^/log/(?P<name>\w+)/?$', LogHandler),
    (r'^/update/?$', UpdateHandler),
    (r'^/power/(?P<op>shutdown|reboot)/?$', PowerHandler),
    (r'^/version/?$', VersionHandler),
    (r'^/login/?$', LoginHandler),
    (r'^.*$', NotFoundHandler),
]


def configure_signals():
    def bye_handler(signal, frame):
        logging.info(_('interrompa signalo ricevita, fermanta …'))

        # shut down the IO loop if it has been started
        io_loop = IOLoop.current()
        io_loop.stop()

    def child_handler(signal, frame):
        # this is required for the multiprocessing mechanism to work
        multiprocessing.active_children()

    signal.signal(signal.SIGINT, bye_handler)
    signal.signal(signal.SIGTERM, bye_handler)
    signal.signal(signal.SIGCHLD, child_handler)


def test_requirements():
    if not os.access(settings.CONF_PATH, os.W_OK):
        logging.fatal(
            'config directory "%s" does not exist or is not writable'
            % settings.CONF_PATH
        )
        sys.exit(-1)

    if not os.access(settings.RUN_PATH, os.W_OK):
        logging.fatal(
            'pid directory "%s" does not exist or is not writable' % settings.RUN_PATH
        )
        sys.exit(-1)

    if not os.access(settings.LOG_PATH, os.W_OK):
        logging.fatal(
            'log directory "%s" does not exist or is not writable' % settings.LOG_PATH
        )
        sys.exit(-1)

    if not os.access(settings.MEDIA_PATH, os.W_OK):
        logging.fatal(
            'media directory "%s" does not exist or is not writable'
            % settings.MEDIA_PATH
        )
        sys.exit(-1)

    if os.geteuid() != 0:
        if settings.SMB_SHARES:
            logging.fatal(_('smb-akcioj postulas radikajn privilegiojn'))
            sys.exit(-1)

    try:
        import tornado  # @UnusedImport

    except ImportError:
        logging.fatal(_('bonvolu instali tornado version 3.1 aŭ pli'))
        sys.exit(-1)

    try:
        import jinja2  # @UnusedImport

    except ImportError:
        logging.fatal(_('bonvolu instali jinja2'))
        sys.exit(-1)

    try:
        import PIL.Image  # @UnusedImport

    except ImportError:
        logging.fatal(_('bonvolu instali pillow aŭ PIL'))
        sys.exit(-1)

    try:
        import pycurl  # @UnusedImport

    except ImportError:
        logging.fatal(_('bonvolu instali pycurl'))
        sys.exit(-1)

    from motioneye import motionctl

    has_motion = motionctl.find_motion()[0] is not None

    from motioneye import mediafiles

    has_ffmpeg = mediafiles.find_ffmpeg() is not None

    has_v4lutils = v4l2ctl.find_v4l2_ctl() is not None

    if settings.SMB_SHARES and smbctl.find_mount_cifs() is None:
        logging.fatal('please install cifs-utils')
        sys.exit(-1)

    if not has_motion:
        logging.info('motion not installed')

    if not has_ffmpeg:
        if has_motion:
            logging.warning('you have motion installed, but no ffmpeg')

        else:
            logging.info('ffmpeg not installed')

    if not has_v4lutils:
        if has_motion:
            logging.warning('you have motion installed, but no v4l-utils')

        else:
            logging.info('v4l-utils not installed')


def make_media_folders():
    from motioneye import config

    config.get_main()  # just to have main config already loaded

    camera_ids = config.get_camera_ids()
    for camera_id in camera_ids:
        camera_config = config.get_camera(camera_id)
        if 'target_dir' in camera_config:
            if not os.path.exists(camera_config['target_dir']):
                try:
                    os.makedirs(camera_config['target_dir'])

                except Exception as e:
                    logging.error(
                        'failed to create root media folder "{}" for camera with id {}: {}'.format(
                            camera_config['target_dir'], camera_id, e
                        )
                    )


def start_motion():
    from motioneye import config, motionctl

    io_loop = IOLoop.current()

    # add a motion running checker
    def checker():

        if (
            not motionctl.running()
            and motionctl.started()
            and config.get_enabled_local_motion_cameras()
        ):
            try:
                logging.error('motion not running, starting it')
                motionctl.start()

            except Exception as e:
                logging.error(f'failed to start motion: {str(e)}', exc_info=True)

        io_loop.add_timeout(
            datetime.timedelta(seconds=settings.MOTION_CHECK_INTERVAL), checker
        )

    try:
        motionctl.start()

    except Exception as e:
        logging.error(str(e), exc_info=True)

    io_loop.add_timeout(
        datetime.timedelta(seconds=settings.MOTION_CHECK_INTERVAL), checker
    )


def parse_options(parser, args):
    parser.add_argument(
        '-b',
        help='start the server in background (daemonize)',
        action='store_true',
        dest='background',
        default=False,
    )

    return parser.parse_args(args)


def make_app(debug: bool = False) -> Application:
    return Application(
        handler_mapping,
        debug=debug,
        log_function=_log_request,
        static_path=settings.STATIC_PATH,
        static_url_prefix='/static/',
    )


def run():
    import motioneye
    from motioneye import cleanup, mjpgclient, motionctl, tasks, wsswitch
    from motioneye.controls import smbctl

    configure_signals()
    logging.info(_('saluton! ĉi tio estas motionEye-servilo ') + motioneye.VERSION)

    test_requirements()
    make_media_folders()

    if settings.SMB_SHARES:
        stop, start = smbctl.update_mounts()  # @UnusedVariable
        if start:
            start_motion()

    else:
        start_motion()

    if settings.CLEANUP_INTERVAL:
        cleanup.start()
        logging.info(_('purigado komenciĝis'))
    wsswitch.start()
    logging.info(_('wsswitch komenciĝis'))

    tasks.start()
    logging.info(_('taskoj komenciĝis'))

    if settings.MJPG_CLIENT_TIMEOUT:
        mjpgclient.start()
        logging.info(_('mjpg klienta rubo-kolektanto komenciĝis'))

    if settings.SMB_SHARES:
        smbctl.start()
        logging.info('smb mounts started')

    template.add_context('static_path', 'static/')
    template.add_context('lingvo', settings.lingvo)

    application = Application(
        handler_mapping,
        debug=False,
        log_function=_log_request,
        static_path=settings.STATIC_PATH,
        static_url_prefix='/static/',
    )

    application.listen(settings.PORT, settings.LISTEN)
    logging.info(_('servilo komenciĝis'))
    io_loop = IOLoop.current()
    # we need to reset the loop's PID to fix PID checks when running in daemon mode
    io_loop._pid = os.getpid()
    io_loop.start()

    logging.info(_('servilo haltis'))
    tasks.stop()
    logging.info(_('taskoj haltis'))

    if cleanup.running():
        cleanup.stop()
        logging.info('cleanup stopped')

    if motionctl.running():
        motionctl.stop()
        logging.info(_('motion haltis'))
    if settings.SMB_SHARES:
        smbctl.stop()
        logging.info('smb mounts stopped')

    logging.info(_('adiaŭ!'))


def main(parser, args, command):
    from motioneye import meyectl

    options = parse_options(parser, args)

    meyectl.configure_logging('motioneye', options.background or options.log_to_file)
    meyectl.configure_tornado()

    if command == 'start':
        if options.background:
            daemon = Daemon(
                pid_file=os.path.join(settings.RUN_PATH, _PID_FILE), run_callback=run
            )
            daemon.start()

        else:
            run()

    elif command == 'stop':
        daemon = Daemon(pid_file=os.path.join(settings.RUN_PATH, _PID_FILE))
        daemon.stop()
