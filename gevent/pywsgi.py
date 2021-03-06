# Copyright (c) 2009-2011, gevent contributors

import errno
import sys
import time
import traceback
from datetime import datetime
from urllib.parse import unquote_to_bytes
try:
    from urllib import unquote
except ImportError:
    from urllib.parse import unquote

from gevent import socket
import gevent
from gevent.server import StreamServer
from gevent.hub import GreenletExit, PY3, string_types, to_wire, to_local
if PY3:
    from gevent._util_py3 import reraise
else:
    from gevent._util_py2 import reraise

__all__ = ['WSGIHandler', 'WSGIServer']

MAX_REQUEST_LINE = 8192
# Weekday and month names for HTTP date/time formatting; always English!
_WEEKDAYNAME = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_MONTHNAME = [None,  # Dummy so we can use 1-based month numbers
              "Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_INTERNAL_ERROR_STATUS = '500 Internal Server Error'
_INTERNAL_ERROR_BODY = 'Internal Server Error'
_INTERNAL_ERROR_HEADERS = [('Content-Type', 'text/plain'),
                           ('Connection', 'close'),
                           ('Content-Length', str(len(_INTERNAL_ERROR_BODY)))]
_REQUEST_TOO_LONG_RESPONSE = "HTTP/1.1 414 Request URI Too Long\r\nConnection: close\r\nContent-length: 0\r\n\r\n"
_BAD_REQUEST_RESPONSE = "HTTP/1.1 400 Bad Request\r\nConnection: close\r\nContent-length: 0\r\n\r\n"
_CONTINUE_RESPONSE = "HTTP/1.1 100 Continue\r\n\r\n"


def format_date_time(timestamp):
    year, month, day, hh, mm, ss, wd, _y, _z = time.gmtime(timestamp)
    return "%s, %02d %3s %4d %02d:%02d:%02d GMT" % (_WEEKDAYNAME[wd], day, _MONTHNAME[month], year, hh, mm, ss)


class Input(object):

    def __init__(self, rfile, content_length, socket=None, chunked_input=False):
        self.rfile = rfile
        self.content_length = content_length
        self.socket = socket
        self.position = 0
        self.chunked_input = chunked_input
        self.chunk_length = -1

    def _discard(self):
        if self.socket is None and (self.position < (self.content_length or 0) or self.chunked_input):
            # ## Read and discard body
            while 1:
                d = self.read(16384)
                if not d:
                    break

    def _send_100_continue(self):
        if self.socket is not None:
            self.socket.sendall(_CONTINUE_RESPONSE)
            self.socket = None

    def _do_read(self, length=None, use_readline=False):
        if use_readline:
            reader = self.rfile.readline
        else:
            reader = self.rfile.read
        content_length = self.content_length
        if content_length is None:
            # Either Content-Length or "Transfer-Encoding: chunked" must be present in a request with a body
            # if it was chunked, then this function would have not been called
            return to_wire('')
        self._send_100_continue()
        left = content_length - self.position
        if length is None:
            length = left
        elif length > left or length < 0:
            length = left
        if not length:
            return to_wire('')
        read = to_local(reader(length))
        self.position += len(read)
        if len(read) < length:
            if (use_readline and not read.endswith("\n")) or not use_readline:
                raise IOError("unexpected end of file while reading request at position %s" % (self.position,))

        return to_wire(read)

    def _chunked_read(self, length=None, use_readline=False):
        rfile = self.rfile
        self._send_100_continue()

        if not length == None and length == 0:
            return to_wire("")

        if not length == None and length < 0:
            length = None

        if use_readline:
            reader = self.rfile.readline
        else:
            reader = self.rfile.read

        response = []
        while self.chunk_length != 0:
            maxreadlen = self.chunk_length - self.position
            if length is not None and length < maxreadlen:
                maxreadlen = length

            if maxreadlen > 0:
                data = to_local(reader(maxreadlen))
                if not data:
                    self.chunk_length = 0
                    raise IOError("unexpected end of file while parsing chunked data")

                datalen = len(data)
                response.append(data)

                self.position += datalen
                if self.chunk_length == self.position:
                    rfile.readline()

                if length is not None:
                    length -= datalen
                    if length == 0:
                        break
                if use_readline and data[-1] == "\n":
                    break
            else:
                line = to_local(rfile.readline())
                if not line.endswith("\n"):
                    self.chunk_length = 0
                    raise IOError("unexpected end of file while reading chunked data header")
                self.chunk_length = int(line.split(";", 1)[0], 16)
                self.position = 0
                if self.chunk_length == 0:
                    rfile.readline()
        return to_wire(''.join(response))

    def read(self, length=None):
        if self.chunked_input:
            return self._chunked_read(length)
        return self._do_read(length)

    def readline(self, size=None):
        if self.chunked_input:
            return self._chunked_read(size, True)
        else:
            return self._do_read(size, use_readline=True)

    def readlines(self, hint=None):
        return list(self)

    def __iter__(self):
        return self

    def next(self):
        line = self.readline()
        if not line:
            raise StopIteration
        return line
    
    if PY3:
        __next__ = next
        del next

try:
    import mimetools
    headers_factory = mimetools.Message
except ImportError:
    # adapt Python 3 HTTP headers to old API
    from http import client
    from email.feedparser import FeedParser

    class OldMessage(client.HTTPMessage):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self.status = ''

        def getheader(self, name, default=None):
            return self.get(name, default)

        @property
        def headers(self):
            for key, value in self._headers:
                yield '%s: %s\r\n' % (key, value)

        @property
        def typeheader(self):
            return self.get('content-type')

    def headers_factory(_, fp, *args):
        headers = 0
        feedparser = FeedParser(OldMessage)
        try:
            while True:
                line = to_local(fp.readline(client._MAXLINE + 1))
                if len(line) > client._MAXLINE:
                    ret = OldMessage()
                    ret.status = 'Line too long'
                    return ret
                headers += 1
                if headers > client._MAXHEADERS:
                    raise client.HTTPException("got more than %d headers" % client._MAXHEADERS)
                feedparser.feed(line)
                if line in ('\r\n', '\n', ''):
                    return feedparser.close()
        finally:
            # break the recursive reference chain
            feedparser.__dict__.clear()


class WSGIHandler(object):
    protocol_version = 'HTTP/1.1'
    MessageClass = headers_factory

    def __init__(self, socket, address, server, rfile=None):
        self.socket = socket
        self.client_address = address
        self.server = server
        if rfile is None:
            self.rfile = socket.makefile('rb', -1)
        else:
            self.rfile = rfile

    def handle(self):
        try:
            while self.socket is not None:
                self.time_start = time.time()
                self.time_finish = 0
                result = self.handle_one_request()
                if result is None:
                    break
                if result is True:
                    continue
                self.status, response_body = result
                self.socket.sendall(response_body)
                if self.time_finish == 0:
                    self.time_finish = time.time()
                self.log_request()
                break
        finally:
            if self.socket is not None:
                try:
                    # read out request data to prevent error: [Errno 104] Connection reset by peer
                    try:
                        if PY3:
                            super(socket.socket, self.socket).recv(16384)
                        else:
                            self.socket._sock.recv(16384)
                    finally:
                        # sleep 0.001 to prevent error: [Errno 54] Connection reset by peer
                        gevent.sleep(0.001)
                        self.rfile.close()
                        if not PY3:
                            self.socket._sock.close()  # do not rely on garbage collection
                        self.socket.close()
                except socket.error:
                    pass
            self.__dict__.pop('socket', None)
            self.__dict__.pop('rfile', None)

    def _check_http_version(self):
        version = self.request_version
        if not version.startswith("HTTP/"):
            return False
        version = tuple(int(x) for x in version[5:].split("."))  # "HTTP/"
        if version[1] < 0 or version < (0, 9) or version >= (2, 0):
            return False
        return True

    def read_request(self, raw_requestline):
        raw_requestline = to_local(raw_requestline)
        self.requestline = raw_requestline.rstrip()
        words = self.requestline.split()
        if len(words) == 3:
            self.command, self.path, self.request_version = words
            if not self._check_http_version():
                self.log_error('Invalid http version: %r', raw_requestline)
                return
        elif len(words) == 2:
            self.command, self.path = words
            if self.command != "GET":
                self.log_error('Expected GET method: %r', raw_requestline)
                return
            self.request_version = "HTTP/0.9"
            # QQQ I'm pretty sure we can drop support for HTTP/0.9
        else:
            self.log_error('Invalid HTTP method: %r', raw_requestline)
            return

        self.headers = self.MessageClass(self.rfile, 0)
        if self.headers.status:
            self.log_error('Invalid headers status: %r', self.headers.status)
            return

        if self.headers.get("transfer-encoding", "").lower() == "chunked":
            try:
                del self.headers["content-length"]
            except KeyError:
                pass

        content_length = self.headers.get("content-length")
        if content_length is not None:
            content_length = int(content_length)
            if content_length < 0:
                self.log_error('Invalid Content-Length: %r', content_length)
                return
            if content_length and self.command in ('HEAD', ):
                self.log_error('Unexpected Content-Length')
                return

        self.content_length = content_length

        if self.request_version == "HTTP/1.1":
            conntype = self.headers.get("Connection", "").lower()
            if conntype == "close":
                self.close_connection = True
            else:
                self.close_connection = False
        else:
            self.close_connection = True

        return True

    def log_error(self, msg, *args):
        try:
            message = msg % args
        except Exception:
            traceback.print_exc()
            message = '%r %r' % (msg, args)
        try:
            message = '%s: %s' % (self.socket, message)
        except Exception:
            pass
        try:
            sys.stderr.write(message + '\n')
        except Exception:
            traceback.print_exc()

    def read_requestline(self):
        return self.rfile.readline(MAX_REQUEST_LINE)

    def handle_one_request(self):
        if self.rfile.closed:
            return

        try:
            self.requestline = to_local(self.read_requestline())
        except socket.error:
            # "Connection reset by peer" or other socket errors aren't interesting here
            return

        if not self.requestline:
            return

        self.response_length = 0

        if len(self.requestline) >= MAX_REQUEST_LINE:
            return ('414', _REQUEST_TOO_LONG_RESPONSE)

        try:
            # for compatibility with older versions of pywsgi, we pass self.requestline as an argument there
            if not self.read_request(self.requestline):
                return ('400', _BAD_REQUEST_RESPONSE)
        except Exception as ex:
            if not isinstance(ex, ValueError):
                traceback.print_exc()
            self.log_error('Invalid request: %s', str(ex) or ex.__class__.__name__)
            return ('400', _BAD_REQUEST_RESPONSE)

        self.environ = self.get_environ()
        self.application = self.server.application
        try:
            self.handle_one_response()
        except socket.error as ex:
            # Broken pipe, connection reset by peer
            if ex.args[0] in (errno.EPIPE, errno.ECONNRESET):
                if not PY3:
                    sys.exc_clear()
                return
            else:
                raise

        if self.close_connection:
            return

        if self.rfile.closed:
            return

        return True  # read more requests

    def finalize_headers(self):
        if self.provided_date is None:
            self.response_headers.append(('Date', format_date_time(time.time())))

        if self.code not in (304, 204):
            # the reply will include message-body; make sure we have either Content-Length or chunked
            if self.provided_content_length is None:
                if hasattr(self.result, '__len__'):
                    self.response_headers.append(('Content-Length', str(sum(len(chunk) for chunk in self.result))))
                else:
                    if self.request_version != 'HTTP/1.0':
                        self.response_use_chunked = True
                        self.response_headers.append(('Transfer-Encoding', 'chunked'))

    def _sendall(self, data):
        try:
            self.socket.sendall(data)
        except socket.error as ex:
            self.status = 'socket error: %s' % ex
            if self.code > 0:
                self.code = -self.code
            raise
        self.response_length += len(data)

    def _write(self, data):
        if not data:
            return
        if self.response_use_chunked:
            ## Write the chunked encoding
            data = to_local(data)
            data = "%x\r\n%s\r\n" % (len(data), data)
        self._sendall(data)

    def write(self, data):
        if self.code in (304, 204) and data:
            raise AssertionError('The %s response must have no body' % self.code)

        if self.headers_sent:
            self._write(data)
        else:
            if not self.status:
                raise AssertionError("The application did not call start_response()")
            self._write_with_headers(data)

    def _write_with_headers(self, data):
        towrite = bytearray()
        self.headers_sent = True
        self.finalize_headers()

        towrite.extend(to_wire('HTTP/1.1 %s\r\n' % self.status))
        for header in self.response_headers:
            towrite.extend(to_wire('%s: %s\r\n' % header))

        towrite.extend(to_wire('\r\n'))
        if data:
            data = to_local(data)
            if self.response_use_chunked:
                towrite.extend(to_wire("%x\r\n%s\r\n" % (len(data), data)))
            else:
                towrite.extend(to_wire(data))
        self._sendall(towrite)

    def start_response(self, status, headers, exc_info=None):
        if exc_info:
            try:
                if self.headers_sent:
                    # Re-raise original exception if headers sent
                    reraise(*exc_info)
            finally:
                # Avoid dangling circular ref
                exc_info = None
        self.code = int(status.split(' ', 1)[0])
        self.status = status
        self.response_headers = headers

        provided_connection = None
        self.provided_date = None
        self.provided_content_length = None

        for header, value in headers:
            header = header.lower()
            if header == 'connection':
                provided_connection = value
            elif header == 'date':
                self.provided_date = value
            elif header == 'content-length':
                self.provided_content_length = value

        if self.request_version == 'HTTP/1.0' and provided_connection is None:
            headers.append(('Connection', 'close'))
            self.close_connection = True
        elif provided_connection == 'close':
            self.close_connection = True

        if self.code in (304, 204):
            if self.provided_content_length is not None and self.provided_content_length != '0':
                msg = 'Invalid Content-Length for %s response: %r (must be absent or zero)' % (self.code, self.provided_content_length)
                raise AssertionError(msg)

        return self.write

    def log_request(self):
        log = self.server.log
        if log:
            log.write(self.format_request() + '\n')

    def format_request(self):
        now = datetime.now().replace(microsecond=0)
        length = self.response_length or '-'
        if self.time_finish:
            delta = '%.6f' % (self.time_finish - self.time_start)
        else:
            delta = '-'
        client_address = self.client_address[0] if isinstance(self.client_address, tuple) else self.client_address
        return '%s - - [%s] "%s" %s %s %s' % (
            client_address or '-',
            now,
            getattr(self, 'requestline', ''),
            (getattr(self, 'status', None) or '000').split()[0],
            length,
            delta)

    def process_result(self):
        for data in self.result:
            if data:
                self.write(data)
        if self.status and not self.headers_sent:
            self.write('')
        if self.response_use_chunked:
            self.socket.sendall('0\r\n\r\n')
            self.response_length += 5

    def run_application(self):
        self.result = self.application(self.environ, self.start_response)
        self.process_result()

    def handle_one_response(self):
        self.time_start = time.time()
        self.status = None
        self.headers_sent = False

        self.result = None
        self.response_use_chunked = False
        self.response_length = 0

        try:
            try:
                self.run_application()
            finally:
                close = getattr(self.result, 'close', None)
                if close is not None:
                    close()
                self.wsgi_input._discard()
        except:
            self.handle_error(*sys.exc_info())
        finally:
            self.time_finish = time.time()
            self.log_request()

    def handle_error(self, type, value, tb):
        if not issubclass(type, GreenletExit):
            self.server.loop.handle_error(self.environ, type, value, tb)
        del tb
        if self.response_length:
            self.close_connection = True
        else:
            self.start_response(_INTERNAL_ERROR_STATUS, _INTERNAL_ERROR_HEADERS[:])
            self.write(_INTERNAL_ERROR_BODY)

    def _headers(self):
        key = None
        value = None
        for header in self.headers.headers:
            if key is not None and header[:1] in " \t":
                value += header
                continue

            if key not in (None, 'CONTENT_TYPE', 'CONTENT_LENGTH'):
                yield 'HTTP_' + key, value.strip()

            key, value = header.split(':', 1)
            key = key.replace('-', '_').upper()

        if key not in (None, 'CONTENT_TYPE', 'CONTENT_LENGTH'):
            yield 'HTTP_' + key, value.strip()

    def get_environ(self):
        env = self.server.get_environ()
        env['REQUEST_METHOD'] = self.command
        env['SCRIPT_NAME'] = ''

        if '?' in self.path:
            path, query = self.path.split('?', 1)
        else:
            path, query = self.path, ''
        env['PATH_INFO'] = unquote(path) if not PY3 else to_local(unquote_to_bytes(path))
        env['QUERY_STRING'] = query

        if self.headers.typeheader is not None:
            env['CONTENT_TYPE'] = self.headers.typeheader

        length = self.headers.getheader('content-length')
        if length:
            env['CONTENT_LENGTH'] = length
        env['SERVER_PROTOCOL'] = self.request_version

        client_address = self.client_address
        if isinstance(client_address, tuple):
            env['REMOTE_ADDR'] = str(client_address[0])
            env['REMOTE_PORT'] = str(client_address[1])

        for key, value in self._headers():
            if key in env:
                if 'COOKIE' in key:
                    env[key] += '; ' + value
                else:
                    env[key] += ',' + value
            else:
                env[key] = value

        if env.get('HTTP_EXPECT') == '100-continue':
            socket = self.socket
        else:
            socket = None
        chunked = env.get('HTTP_TRANSFER_ENCODING', '').lower() == 'chunked'
        self.wsgi_input = Input(self.rfile, self.content_length, socket=socket, chunked_input=chunked)
        env['wsgi.input'] = self.wsgi_input
        return env


class WSGIServer(StreamServer):
    """A WSGI server based on :class:`StreamServer` that supports HTTPS."""

    handler_class = WSGIHandler
    base_env = {'GATEWAY_INTERFACE': 'CGI/1.1',
                'SERVER_SOFTWARE': 'gevent/%d.%d Python/%d.%d' % (gevent.version_info[:2] + sys.version_info[:2]),
                'SCRIPT_NAME': '',
                'wsgi.version': (1, 0),
                'wsgi.multithread': False,
                'wsgi.multiprocess': False,
                'wsgi.run_once': False}

    def __init__(self, listener, application=None, backlog=None, spawn='default', log='default', handler_class=None,
                 environ=None, **ssl_args):
        StreamServer.__init__(self, listener, backlog=backlog, spawn=spawn, **ssl_args)
        if application is not None:
            self.application = application
        if handler_class is not None:
            self.handler_class = handler_class
        if log == 'default':
            self.log = sys.stderr
        else:
            self.log = log
        self.set_environ(environ)
        self.set_max_accept()

    def set_environ(self, environ=None):
        if environ is not None:
            self.environ = environ
        environ_update = getattr(self, 'environ', None)
        self.environ = self.base_env.copy()
        if self.ssl_enabled:
            self.environ['wsgi.url_scheme'] = 'https'
        else:
            self.environ['wsgi.url_scheme'] = 'http'
        if environ_update is not None:
            self.environ.update(environ_update)
        if self.environ.get('wsgi.errors') is None:
            self.environ['wsgi.errors'] = sys.stderr

    def set_max_accept(self):
        if self.environ.get('wsgi.multiprocess'):
            self.max_accept = 1

    def get_environ(self):
        return self.environ.copy()

    def init_socket(self):
        StreamServer.init_socket(self)
        self.update_environ()

    def update_environ(self):
        address = self.address
        if isinstance(address, tuple):
            if 'SERVER_NAME' not in self.environ:
                try:
                    name = socket.getfqdn(address[0])
                except socket.error:
                    name = str(address[0])
                self.environ['SERVER_NAME'] = name
            self.environ.setdefault('SERVER_PORT', str(address[1]))
        else:
            self.environ.setdefault('SERVER_NAME', '')
            self.environ.setdefault('SERVER_PORT', '')

    def handle(self, socket, address):
        handler = self.handler_class(socket, address, self)
        handler.handle()
