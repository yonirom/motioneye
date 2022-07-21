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

import json
import logging
import urllib.error
import urllib.parse
import urllib.request

import paho.mqtt.publish as publish
from motioneye import settings


def parse_options(parser, args):
    parser.add_argument('message', help='mqtt message')
    parser.add_argument('url', help='the URL for the request')

    return parser.parse_args(args)


def main(parser, args):
    from motioneye import meyectl, utils

    options = parse_options(parser, args)

    meyectl.configure_logging('mqtt', options.log_to_file)
    meyectl.configure_tornado()

    logging.debug('hello!')
    logging.debug('url = %s' % options.url)
    logging.debug('method = %s' % options.message)

    parts = urllib.parse.urlparse(options.url)
    auth = None
    if parts.username:
        auth = {
                'username': parts.username,
                'passowrd': parts.password
               }

    try:
        publish.single(parts.path[1:], options.message, hostname=parts.hostname, auth=auth)
    except Exception as e:
        logging.error('failed to pusblish mqtt: %s' % e)

    logging.debug('bye!')
