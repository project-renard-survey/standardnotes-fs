import argparse
from configparser import ConfigParser
from getpass import getpass
import logging
import sys
import os
import pathlib
import platform
import subprocess

import appdirs
from fuse import FUSE
from requests.exceptions import ConnectionError, MissingSchema

from standardnotes_fs.api import SNAPIException, StandardNotesAPI
from standardnotes_fs.sn_fuse import StandardNotesFUSE

OFFICIAL_SERVER_URL = 'https://sync.standardnotes.org'
DEFAULT_SYNC_SEC = 30
MINIMUM_SYNC_SEC = 5
DEFAULT_EXT = '.txt'
APP_NAME = 'standardnotes-fs'

# path settings
cfg_env = os.environ.get('SN_FS_CONFIG_PATH')
CONFIG_PATH = cfg_env if cfg_env else appdirs.user_config_dir(APP_NAME)
CONFIG_FILE = pathlib.PurePath(CONFIG_PATH, APP_NAME + '.conf')
cfg_env = os.environ.get('SN_FS_CREDS_PATH')
CREDS_PATH = cfg_env if cfg_env else appdirs.user_cache_dir(APP_NAME)
CREDS_FILE = pathlib.PurePath(CREDS_PATH, APP_NAME + '.conf')

def parse_options():
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument('mountpoint', nargs='?', help='local mountpoint folder')
    parser.add_argument('--username',
                        help='Standard Notes username to log in with')
    parser.add_argument('--password',
                        help='Standard Notes password to log in with\n'
                             'NOTE: It is NOT recommended to use this option!\n'
                             '      The password may be stored in history, so\n'
                             '      use the password prompt instead.')
    parser.add_argument('-v', '--verbosity', action='count',
                        help='output verbosity -v or -vv (implies --foreground)')
    parser.add_argument('--foreground', action='store_true',
                        help='run standardnotes-fs in the foreground')
    parser.add_argument('--sync-sec', type=int, default=DEFAULT_SYNC_SEC,
                        help='how many seconds between each sync. Default: '
                        ''+str(DEFAULT_SYNC_SEC))
    parser.add_argument('--sync-url',
                        help='URL of Standard File sync server. Defaults to:\n'
                        ''+OFFICIAL_SERVER_URL)
    parser.add_argument('--ext', default=DEFAULT_EXT,
                        help='file extension to add to note titles. Default: '
                        ''+DEFAULT_EXT)
    parser.add_argument('--no-config-files', action='store_true',
                        help='don\'t load or create config / cred files')
    parser.add_argument('--config', default=CONFIG_FILE,
                        help='specify a config file location. Defaults to:\n'
                        ''+str(CONFIG_FILE))
    parser.add_argument('--creds', default=CREDS_FILE,
                        help='specify a credentials file location. Defaults to:\n'
                        ''+str(CREDS_FILE))
    parser.add_argument('--logout', action='store_true',
                        help='remove config files and user credentials')
    parser.add_argument('-u', '--unmount', action='store_true',
                        help='unmount [mountpoint] folder')
    return parser.parse_args()

def main():
    args = parse_options()
    config = ConfigParser()
    creds = ConfigParser()
    keys = {}
    login_success = False

    # configure logging
    if args.verbosity == 1:
        log_level = logging.INFO
    elif args.verbosity == 2:
        log_level = logging.DEBUG
    else:
        log_level = logging.ERROR
    logging.basicConfig(level=log_level,
                        format='%(levelname)-8s: %(message)s')
    if args.verbosity: args.foreground = True

    # figure out config files
    config_file = pathlib.Path(args.config)
    creds_file = pathlib.Path(args.creds)

    # remove config and quit if wanted
    if args.logout:
        try:
            config_file.unlink()
        except OSError:
            logging.info('No config file found.')
        try:
            creds_file.unlink()
        except OSError:
            logging.info('No creds file found.')
        print('Config files removed.')
        if not args.unmount: sys.exit(0)

    # make sure mountpoint is specified
    if not args.mountpoint:
        print('No mountpoint specified.')
        sys.exit(1)

    # unmount folder and quit if wanted
    if args.unmount:
        try:
            if platform.system().lower() != 'darwin':
                umount_cmd = ['fusermount', '-u']
            else:
                umount_cmd = ['umount']
            subprocess.check_call(umount_cmd + [args.mountpoint])
            print(args.mountpoint + ' unmounted.')
        except:
            print('Error unmounting file system.')
            sys.exit(1)
        sys.exit(0)

    # keep sync_sec above the minimum sync time
    if args.sync_sec < MINIMUM_SYNC_SEC:
        sync_sec = MINIMUM_SYNC_SEC
        print('Sync interval must be at least', MINIMUM_SYNC_SEC,
              'seconds. Using that instead.')
    else:
        sync_sec = args.sync_sec

    # load config file settings
    if not args.no_config_files:
        try:
            config_file.parent.mkdir(mode=0o0700, parents=True)
        except FileExistsError:
            pass
        except OSError:
            log_msg = 'Error creating config file directory "%s".'
            print(log_msg % str(config_file.parent))
            sys.exit(1)
        finally:
            log_msg = 'Using config directory "%s".'
            logging.info(log_msg % str(config_file.parent))

        try:
            with config_file.open() as f:
                config.read_file(f)
                log_msg = 'Loaded config file "%s".'
                logging.info(log_msg % str(config_file))
        except OSError:
            log_msg = 'Unable to read config file "%s".'
            logging.info(log_msg % str(config_file))

    # load creds file settings
    if not args.no_config_files:
        try:
            creds_file.parent.mkdir(mode=0o0700, parents=True)
        except FileExistsError:
            pass
        except OSError:
            log_msg = 'Error creating creds file directory "%s".'
            print(log_msg % str(creds_file.parent))
            sys.exit(1)
        finally:
            log_msg = 'Using creds directory "%s".'
            logging.info(log_msg % str(creds_file.parent))

        try:
            with creds_file.open() as f:
                creds.read_file(f)
                log_msg = 'Loaded creds file "%s".'
                logging.info(log_msg % str(creds_file))
        except OSError:
            log_msg = 'Unable to read creds file "%s".'
            logging.info(log_msg % str(creds_file))

    # figure out all login params
    if args.sync_url:
        sync_url = args.sync_url
    elif config.has_option('user', 'sync_url'):
        sync_url = config.get('user', 'sync_url')
    else:
        sync_url = OFFICIAL_SERVER_URL
    log_msg = 'Using sync URL "%s".'
    logging.info(log_msg % sync_url)

    if (config.has_option('user', 'username')
        and creds.has_section('keys')
        and not args.username
        and not args.password):
            username = config.get('user', 'username')
            keys = dict(creds.items('keys'))
    else:
        username = (args.username if args.username else
                    input('Please enter your Standard Notes username: '))
        password = (args.password if args.password else
                    getpass('Please enter your password (hidden): '))

    # log the user in
    try:
        sn_api = StandardNotesAPI(sync_url, username)
        if not keys:
            keys = sn_api.gen_keys(password)
            del password
        sn_api.sign_in(keys)
        log_msg = 'Successfully logged into account "%s".'
        logging.info(log_msg % username)
        login_success = True
    except SNAPIException as e:
        print(e)
    except ConnectionError:
        log_msg = 'Unable to connect to the sync server at "%s".'
        print(log_msg % sync_url)
        sys.exit(1)
    except MissingSchema:
        log_msg = 'Invalid sync server url "%s".'
        print(log_msg % sync_url)
        sys.exit(1)

    # write config back if good, clear if not
    if not args.no_config_files:
        try:
            with config_file.open(mode='w+') as f:
                if login_success:
                    config.read_dict(dict(user=dict(sync_url=sync_url,
                                                    username=username),
                                          ))
                    config.remove_section('keys')
                    config.write(f)
                    log_msg = 'Config written to file "%s".'
                else:
                    log_msg = 'Clearing config file "%s".'
                logging.info(log_msg % config_file)
            config_file.chmod(0o600)
        except OSError:
            log_msg = 'Unable to write config file "%s".'
            print(log_msg % str(config_file))

    # write creds back if good, clear if not
    if not args.no_config_files:
        try:
            with creds_file.open(mode='w+') as f:
                if login_success:
                    creds.read_dict(dict(keys=keys))
                    creds.write(f)
                    log_msg = 'Creds written to file "%s".'
                else:
                    log_msg = 'Clearing creds file "%s".'
                logging.info(log_msg % creds_file)
            creds_file.chmod(0o600)
        except OSError:
            log_msg = 'Unable to write creds file "%s".'
            print(log_msg % str(creds_file))

    if login_success:
        logging.info('Starting FUSE filesystem.')
        try:
            fuse = FUSE(StandardNotesFUSE(sn_api, sync_sec, args.ext),
                        args.mountpoint, use_ino=True,
                        foreground=args.foreground,
                        nothreads=True) # FUSE can't make threads, but we can
        except RuntimeError as e:
            print('Error mounting file system.')

    logging.info('Exiting.')

if __name__ == '__main__':
    main()
