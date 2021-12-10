# coding: utf-8
import logging
from collections import OrderedDict
from typing import List, Dict

from django.core.exceptions import ImproperlyConfigured
from django.utils.functional import cached_property

from modernrpc.conf import settings
from modernrpc.helpers import ensure_sequence
from modernrpc.introspection import Introspector, DocstringParser

# Special constant meaning "all protocols" or "all entry points"
ALL = "__all__"

# Protocols identifiers
JSONRPC_PROTOCOL = '__json_rpc'
XMLRPC_PROTOCOL = '__xml_rpc'

# Keys used in kwargs dict given to RPC methods
REQUEST_KEY = settings.MODERNRPC_KWARGS_REQUEST_KEY
ENTRY_POINT_KEY = settings.MODERNRPC_KWARGS_ENTRY_POINT_KEY
PROTOCOL_KEY = settings.MODERNRPC_KWARGS_PROTOCOL_KEY
HANDLER_KEY = settings.MODERNRPC_KWARGS_HANDLER_KEY

logger = logging.getLogger(__name__)


class RPCMethod:

    def __init__(self, func):
        # Store the reference to the registered function
        self.function = func

        # @rpc_method decorator parameters
        self.entry_point = getattr(func, 'modernrpc_entry_point')
        self.protocol = getattr(func, 'modernrpc_protocol')

        # Authentication related attributes
        self.predicates = getattr(func, 'modernrpc_auth_predicates', None)
        self.predicates_params = getattr(func, 'modernrpc_auth_predicates_params', ())

        # Introspection
        self.introspector = Introspector(self.function)
        self.doc_parser = DocstringParser(self.function)

    @property
    def name(self):
        return getattr(self.function, 'modernrpc_name', self.function.__name__)

    def __repr__(self):
        return 'RPC Method ' + self.name

    def __str__(self):
        return '{}({})'.format(self.name, ', '.join(self.introspector.args))

    def __eq__(self, other):
        return \
            self.function == other.function and \
            self.name == other.name and \
            self.entry_point == other.entry_point and \
            self.protocol == other.protocol and \
            self.predicates == other.predicates and \
            self.predicates_params == other.predicates_params

    def check_permissions(self, request):
        """Call the predicate(s) associated with the RPC method, to check if the current request
        can actually call the method.
        Return a boolean indicating if the method should be executed (True) or not (False)"""
        if not self.predicates:
            return True

        # All registered authentication predicates must return True
        return all(
            predicate(request, *self.predicates_params[i])
            for i, predicate in enumerate(self.predicates)
        )

    def available_for_protocol(self, protocol):
        """Check if the current function can be executed from a request through the given protocol"""
        if ALL in (self.protocol, protocol):
            return True
        return protocol in ensure_sequence(self.protocol)

    def available_for_entry_point(self, entry_point):
        """Check if the current function can be executed from a request to the given entry point"""
        if ALL in (self.entry_point, entry_point):
            return True
        return entry_point in ensure_sequence(self.entry_point)

    def is_valid_for(self, entry_point, protocol):
        """Check if the current function can be executed from a request to the given entry point
        and with the given protocol"""
        return self.available_for_entry_point(entry_point) and self.available_for_protocol(protocol)

    def is_available_in_json_rpc(self):
        """Shortcut checking if the current method can be executed on JSON-RPC protocol.
        Used in HTML documentation to easily display protocols supported by an RPC method"""
        return self.available_for_protocol(JSONRPC_PROTOCOL)

    def is_available_in_xml_rpc(self):
        """Shortcut checking if the current method can be executed on XML-RPC protocol.
        Used in HTML documentation to easily display protocols supported by an RPC method"""
        return self.available_for_protocol(XMLRPC_PROTOCOL)

    @cached_property
    def accept_kwargs(self):
        return self.introspector.accept_kwargs

    @cached_property
    def args(self) -> List[str]:
        """Methods arguments"""
        return self.introspector.args

    @cached_property
    def raw_docstring(self) -> str:
        """Methods docstring, as raw text"""
        return self.doc_parser.raw_docstring

    @cached_property
    def html_doc(self) -> str:
        """Methods docstring, as HTML"""
        return self.doc_parser.html_doc

    @cached_property
    def args_doc(self) -> OrderedDict:
        """"""
        result = OrderedDict()
        for arg in self.introspector.args:
            result[arg] = {
                "type": self.doc_parser.args_types.get(arg, "") or self.introspector.args_types.get(arg, ""),
                "text": self.doc_parser.args_doc.get(arg, "")
            }
        return result

    @cached_property
    def return_doc(self) -> Dict[str, str]:
        return {
            "type": self.doc_parser.return_type or self.introspector.return_type,
            "text": self.doc_parser.return_doc
        }


class _RPCRegistry:

    def __init__(self):
        self._registry = {}

    def reset(self):
        self._registry.clear()

    def register_method(self, func):
        """
        Register a function to be available as RPC method.

        The given function will be inspected to find external_name, protocol and entry_point values set by the decorator
        @rpc_method.
        :param func: A function previously decorated using @rpc_method
        :return: The name of registered method
        """
        if not getattr(func, 'modernrpc_enabled', False):
            raise ImproperlyConfigured('Error: trying to register {} as RPC method, but it has not been decorated.'
                                       .format(func.__name__))

        # Define the external name of the function
        name = getattr(func, 'modernrpc_name', func.__name__)

        logger.debug('Register RPC method "%s"', name)

        if name.startswith('rpc.'):
            raise ImproperlyConfigured(
                'According to RPC standard, method names starting with "rpc." are reserved for system extensions and '
                'must not be used. See https://www.jsonrpc.org/specification#extensions for more information.'
            )

        # Encapsulate the function in a RPCMethod object
        method = RPCMethod(func)

        # Ensure method names are unique in the registry
        existing_method = self.get_method(method.name, ALL, ALL)
        if existing_method is not None:
            # Trying to register many times the same function is OK, because if a method is decorated
            # with @rpc_method(), it could be imported in different places of the code
            if method == existing_method:
                return method.name

            # But if we try to use the same name to register 2 different methods, we
            # must inform the developer there is an error in the code
            raise ImproperlyConfigured("A RPC method with name {} has already been registered".format(method.name))

        # Store the method
        self._registry[method.name] = method
        logger.debug('Method registered. len(registry): %d', len(self._registry))

        return method.name

    def total_count(self):
        return len(self._registry)

    def get_all_method_names(self, entry_point=ALL, protocol=ALL, sort_methods=False):
        """Return the names of all RPC methods registered supported by the given entry_point / protocol pair"""

        method_names = [
            name for name, method in self._registry.items() if method.is_valid_for(entry_point, protocol)
        ]

        if sort_methods:
            method_names = sorted(method_names)

        return method_names

    def get_all_methods(self, entry_point=ALL, protocol=ALL, sort_methods=False):
        """Return a list of all methods in the registry supported by the given entry_point / protocol pair"""

        if sort_methods:
            return [
                method for (_, method) in sorted(self._registry.items()) if method.is_valid_for(entry_point, protocol)
            ]

        return self._registry.values()

    def get_method(self, name, entry_point, protocol):
        """Retrieve a method from the given name"""

        if name in self._registry and self._registry[name].is_valid_for(entry_point, protocol):
            return self._registry[name]

        return None


registry = _RPCRegistry()


class RpcRequest:
    """Wrapper for JSON-RPC or XML-RPC request data."""

    def __init__(self, method_name, params=None, **kwargs):
        self.request_id = None
        self.method_name = method_name

        self.args = []
        self.kwargs = {}
        if params is not None:
            if isinstance(params, dict):
                self.kwargs = params
            elif isinstance(params, (list, set, tuple)):
                self.args = params
            else:
                raise ValueError("RPCRequest initial params has an unsupported type: {}".format(type(params)))

        for key, value in kwargs.items():
            setattr(self, key, value)


class RpcResult:

    def __init__(self, request_id=None):
        self.request_id = request_id
        self._response_is_error = None
        self._data = None

    def is_error(self):
        return self._response_is_error

    def set_success(self, data):
        self._response_is_error = False
        self._data = data

    def set_error(self, code, message, data=None):
        self._response_is_error = True
        self._data = (code, message, data)

    @property
    def success_data(self):
        return self._data

    @property
    def error_code(self):
        return self._data[0]

    @property
    def error_message(self):
        return self._data[1]

    @property
    def error_data(self):
        return self._data[2]


def rpc_method(func=None, name=None, entry_point=ALL, protocol=ALL):
    """
    Mark a standard python function as RPC method.

    All arguments are optional

    :param func: A standard function
    :param name: Used as RPC method name instead of original function name
    :param entry_point: Default: ALL. Used to limit usage of the RPC method for a specific set of entry points
    :param protocol: Default: ALL. Used to limit usage of the RPC method for a specific protocol (JSONRPC or XMLRPC)
    :type name: str
    :type entry_point: str
    :type protocol: str
    """

    def decorated(_func):
        _func.modernrpc_enabled = True
        _func.modernrpc_name = name or _func.__name__
        _func.modernrpc_entry_point = entry_point
        _func.modernrpc_protocol = protocol

        return _func

    # If @rpc_method() is used with parenthesis (with or without argument)
    if func is None:
        return decorated

    # If @rpc_method is used without parenthesis
    return decorated(func)


# Backward compatibility.
# In release 0.11.0, following global functions have been moved to a proper _RPCRegistry class,
# instantiated as a global "registry". For backward compatibility
def register_rpc_method(func):
    """For backward compatibility. Use registry.register_method() instead (with same arguments)"""
    return registry.register_method(func)


def get_all_method_names(entry_point=ALL, protocol=ALL, sort_methods=False):
    """For backward compatibility. Use registry.get_all_method_names() instead (with same arguments)"""
    return registry.get_all_method_names(entry_point=entry_point, protocol=protocol, sort_methods=sort_methods)


def get_all_methods(entry_point=ALL, protocol=ALL, sort_methods=False):
    """For backward compatibility. Use registry.get_all_methods() instead (with same arguments)"""
    return registry.get_all_methods(entry_point=entry_point, protocol=protocol, sort_methods=sort_methods)


def get_method(name, entry_point, protocol):
    """For backward compatibility. Use registry.get_method() instead (with same arguments)"""
    return registry.get_method(name, entry_point, protocol)


def reset_registry():
    """For backward compatibility. Use registry.reset() instead"""
    return registry.reset()
