"""Run Spatial Ledger locally with a persistent SQLite point index."""
import argparse
import json
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from spatial import Store, Conflict, Missing, SAMPLES

ROOT = Path(__file__).resolve().parent
ASSETS = {'/': ('index.html','text/html'), '/style.css': ('style.css','text/css'), '/app.js': ('app.js','text/javascript')}


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, value, mime='application/json'):
        body = value if isinstance(value, bytes) else json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', mime+'; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def handle_request(self):
        try:
            hosts = self.headers.get_all('Host', [])
            if len(hosts) != 1 or not re.fullmatch(r'(localhost|127\.0\.0\.1)(:[0-9]{1,5})?', hosts[0], re.I):
                return self.reply(400, {'error': 'Use a loopback Host header.'})
            path = urlsplit(self.path).path
            if self.command in ('GET','HEAD'):
                if path in ASSETS:
                    name,mime = ASSETS[path]
                    return self.reply(200,(ROOT/name).read_bytes(),mime)
                if path == '/api/points':
                    return self.reply(200, {'points': self.server.store.list()})
                if path == '/api/samples':
                    return self.reply(200, {'points': SAMPLES})
                return self.reply(404, {'error': 'Not found.'})
            if self.command != 'POST':
                return self.reply(405, {'error': 'Method not supported.'})
            origin = self.headers.get('Origin')
            port = self.server.server_port
            if origin is not None and origin not in (f'http://127.0.0.1:{port}',f'http://localhost:{port}'):
                return self.reply(403, {'error': 'Cross-origin requests are rejected.'})
            lengths = self.headers.get_all('Content-Length', [])
            if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not lengths[0].isdigit() or not 0 < int(lengths[0]) <= 400000:
                return self.reply(413, {'error': 'Provide a JSON body of at most 400 KB.'})
            if self.headers.get('Content-Type','').split(';')[0].strip() != 'application/json':
                return self.reply(415, {'error': 'Use application/json.'})
            body = json.loads(self.rfile.read(int(lengths[0])).decode('utf-8'))
            if not isinstance(body,dict):
                raise ValueError('Request body must be an object.')
            if path == '/api/points' and set(body) == {'points'}:
                return self.reply(201, {'ids': self.server.store.add(body['points'])})
            if path == '/api/search' and set(body) == {'latitude','longitude','radius'}:
                return self.reply(200,self.server.store.search(body['latitude'],body['longitude'],body['radius']))
            match = re.fullmatch(r'/api/points/([1-9][0-9]*)/(edit|archive|restore)',path)
            if match:
                identity,action = int(match[1]),match[2]
                if action == 'edit' and set(body) == {'version','point'}:
                    return self.reply(200,self.server.store.update(identity,body['version'],values=body['point']))
                if action in ('archive','restore') and set(body) == {'version'}:
                    return self.reply(200,self.server.store.update(identity,body['version'],active=action=='restore'))
                raise ValueError('Invalid mutation fields.')
            raise ValueError('Unknown endpoint or unexpected fields.')
        except Missing as error:
            self.reply(404, {'error': str(error)})
        except Conflict as error:
            self.reply(409, {'error': str(error)})
        except (ValueError,UnicodeDecodeError) as error:
            self.reply(400, {'error': str(error)})
        except sqlite3.OperationalError:
            self.reply(503, {'error': 'Database temporarily unavailable. Retry shortly.'})

    do_GET = handle_request
    do_HEAD = handle_request
    do_POST = handle_request
    do_PUT = handle_request
    do_DELETE = handle_request


def make_server(path, port=0):
    server = ThreadingHTTPServer(('127.0.0.1',port),Handler)
    server.store = Store(path)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port',type=int,default=8000)
    parser.add_argument('--db',default=str(ROOT/'data/points.sqlite3'))
    args = parser.parse_args()
    server = make_server(args.db,args.port)
    print(f'Spatial Ledger: http://127.0.0.1:{server.server_port}',flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
