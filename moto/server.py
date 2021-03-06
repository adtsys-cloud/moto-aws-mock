from __future__ import unicode_literals
import json
import re
import sys
import argparse
import logging

from six.moves.urllib.parse import urlencode

from threading import Lock

from flask import Flask
from flask.testing import FlaskClient
from werkzeug.routing import BaseConverter
from werkzeug.serving import run_simple

from moto.backends import BACKENDS
from moto.core.utils import convert_flask_to_httpretty_response

HTTP_METHODS = ["GET", "POST", "PUT", "DELETE", "HEAD", "PATCH"]


class DomainDispatcherApplication(object):
    """
    Dispatch requests to different applications based on the "Host:" header
    value. We'll match the host header value with the url_bases of each backend.
    """

    def __init__(self, create_app, service=None):
        self.create_app = create_app
        self.lock = Lock()
        self.app_instances = {}
        self.service = service

    def get_backend_for_host(self, host):
        if self.service:
            return self.service

        if host in BACKENDS:
            return host

        for backend_name, backend in BACKENDS.items():
            for url_base in list(backend.values())[0].url_bases:
                if re.match(url_base, 'http://%s' % host):
                    return backend_name

        raise RuntimeError('Invalid host: "%s"' % host)

    def get_application(self, environ):
        path_info = environ.get('PATH_INFO', '')
        if path_info.startswith("/moto-api") or path_info == "/favicon.ico":
            host = "moto_api"
        elif path_info.startswith("/latest/meta-data/"):
            host = "instance_metadata"
        else:
            host = environ['HTTP_HOST'].split(':')[0]
        if (host == "localhost") or ("adtsys" in host):
            # Fall back to parsing auth header to find service
            # ['Credential=sdffdsa', '20170220', 'us-east-1', 'sns', 'aws4_request']
            try:
                _, _, region, service, _ = environ['HTTP_AUTHORIZATION'].split(",")[0].split()[
                    1].split("/")
            except (KeyError, ValueError):
                region = 'sa-east-1'
                service = 's3'
            if service == 'dynamodb':
                dynamo_api_version = environ['HTTP_X_AMZ_TARGET'].split("_")[1].split(".")[0]
                # If Newer API version, use dynamodb
                if dynamo_api_version > "20111205":
                    host = "dynamodb"
            else:
                host = "{service}.{region}.amazonaws.com".format(
                    service=service, region=region)
            log = logging.getLogger('werkzeug')
            log.debug("HOST:{host}")

        with self.lock:
            backend = self.get_backend_for_host(host)
            app = self.app_instances.get(backend, None)
            if app is None:
                app = self.create_app(backend)
                self.app_instances[backend] = app
            return app

    def __call__(self, environ, start_response):
        backend_app = self.get_application(environ)
        return backend_app(environ, start_response)


class RegexConverter(BaseConverter):
    # http://werkzeug.pocoo.org/docs/routing/#custom-converters

    def __init__(self, url_map, *items):
        super(RegexConverter, self).__init__(url_map)
        self.regex = items[0]


class AWSTestHelper(FlaskClient):

    def action_data(self, action_name, **kwargs):
        """
        Method calls resource with action_name and returns data of response.
        """
        opts = {"Action": action_name}
        opts.update(kwargs)
        res = self.get("/?{0}".format(urlencode(opts)),
                       headers={"Host": "{0}.us-east-1.amazonaws.com".format(self.application.service)})
        return res.data.decode("utf-8")

    def action_json(self, action_name, **kwargs):
        """
        Method calls resource with action_name and returns object obtained via
        deserialization of output.
        """
        return json.loads(self.action_data(action_name, **kwargs))


def create_backend_app(service):
    from werkzeug.routing import Map

    # Create the backend_app
    backend_app = Flask(__name__)
    backend_app.debug = True
    backend_app.service = service

    # Reset view functions to reset the app
    backend_app.view_functions = {}
    backend_app.url_map = Map()
    backend_app.url_map.converters['regex'] = RegexConverter
    backend = list(BACKENDS[service].values())[0]
    for url_path, handler in backend.flask_paths.items():
        if handler.__name__ == 'dispatch':
            endpoint = '{0}.dispatch'.format(handler.__self__.__name__)
        else:
            endpoint = None

        if endpoint in backend_app.view_functions:
            # HACK: Sometimes we map the same view to multiple url_paths. Flask
            # requries us to have different names.
            endpoint += "2"

        backend_app.add_url_rule(
            url_path,
            endpoint=endpoint,
            methods=HTTP_METHODS,
            view_func=convert_flask_to_httpretty_response(handler),
            strict_slashes=False,
        )

    backend_app.test_client_class = AWSTestHelper
    return backend_app


def main(argv=sys.argv[1:]):
    parser = argparse.ArgumentParser()

    # Keep this for backwards compat
    parser.add_argument(
        "service",
        type=str,
        nargs='?',  # http://stackoverflow.com/a/4480202/731592
        default=None)
    parser.add_argument(
        '-H', '--host', type=str,
        help='Which host to bind',
        default='127.0.0.1')
    parser.add_argument(
        '-p', '--port', type=int,
        help='Port number to use for connection',
        default=5000)
    parser.add_argument(
        '-r', '--reload',
        action='store_true',
        help='Reload server on a file change',
        default=False
    )

    args = parser.parse_args(argv)

    # Wrap the main application
    main_app = DomainDispatcherApplication(
        create_backend_app, service=args.service)
    main_app.debug = True
    run_simple(args.host, args.port, main_app, threaded=True, use_reloader=args.reload)
    log = logging.getLogger('werkzeug')
    log.setLevel(logging.DEBUG)


if __name__ == '__main__':
    main()
