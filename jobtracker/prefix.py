"""Mount the entire application at a configured path, without trusting headers."""
from werkzeug.wrappers import Response


class PrefixMount:
    def __init__(self, application, prefix):
        self.application = application
        self.prefix = prefix

    def __call__(self, environ, start_response):
        path = environ.get('PATH_INFO', '')
        if path == self.prefix:
            query = environ.get('QUERY_STRING', '')
            target = self.prefix + '/' + ('?' + query if query else '')
            return Response(status=308, headers={'Location': target})(environ, start_response)
        if not path.startswith(self.prefix + '/'):
            return Response('Not found', status=404)(environ, start_response)
        environ['SCRIPT_NAME'] = environ.get('SCRIPT_NAME', '') + self.prefix
        environ['PATH_INFO'] = path[len(self.prefix):]
        return self.application(environ, start_response)
