from abc import abstractmethod

from parso.tree import search_ancestor

from jedi._compatibility import Parameter
from jedi.inference.base_value import ValueSet, NO_VALUES
from jedi.inference import docstrings
from jedi.cache import memoize_method


class AbstractNameDefinition(object):
    start_pos = None
    string_name = None
    parent_context = None
    tree_name = None
    is_value_name = True
    """
    Used for the Jedi API to know if it's a keyword or an actual name.
    """

    @abstractmethod
    def infer(self):
        raise NotImplementedError

    @abstractmethod
    def goto(self):
        # Typically names are already definitions and therefore a goto on that
        # name will always result on itself.
        return {self}

    def get_qualified_names(self, include_module_names=False):
        qualified_names = self._get_qualified_names()
        if qualified_names is None or not include_module_names:
            return qualified_names

        module_names = self.get_root_context().string_names
        if module_names is None:
            return None
        return module_names + qualified_names

    def _get_qualified_names(self):
        # By default, a name has no qualified names.
        return None

    def get_root_context(self):
        return self.parent_context.get_root_context()

    def get_public_name(self):
        return self.string_name

    def __repr__(self):
        if self.start_pos is None:
            return '<%s: string_name=%s>' % (self.__class__.__name__, self.string_name)
        return '<%s: string_name=%s start_pos=%s>' % (self.__class__.__name__,
                                                      self.string_name, self.start_pos)

    def is_import(self):
        return False

    @property
    def api_type(self):
        return self.parent_context.api_type


class AbstractArbitraryName(AbstractNameDefinition):
    """
    When you e.g. want to complete dicts keys, you probably want to complete
    string literals, which is not really a name, but for Jedi we use this
    concept of Name for completions as well.
    """
    is_value_name = False

    def __init__(self, inference_state, string):
        self.inference_state = inference_state
        self.string_name = string
        self.parent_context = inference_state.builtins_module

    def infer(self):
        return NO_VALUES


class AbstractTreeName(AbstractNameDefinition):
    def __init__(self, parent_context, tree_name):
        self.parent_context = parent_context
        self.tree_name = tree_name

    def get_qualified_names(self, include_module_names=False):
        import_node = search_ancestor(self.tree_name, 'import_name', 'import_from')
        # For import nodes we cannot just have names, because it's very unclear
        # how they would look like. For now we just ignore them in most cases.
        # In case of level == 1, it works always, because it's like a submodule
        # lookup.
        if import_node is not None and not (import_node.level == 1
                                            and self.get_root_context().is_package):
            # TODO improve the situation for when level is present.
            if include_module_names and not import_node.level:
                return tuple(n.value for n in import_node.get_path_for_name(self.tree_name))
            else:
                return None

        return super(AbstractTreeName, self).get_qualified_names(include_module_names)

    def _get_qualified_names(self):
        parent_names = self.parent_context.get_qualified_names()
        if parent_names is None:
            return None
        return parent_names + (self.tree_name.value,)

    def goto(self, **kwargs):
        return self.parent_context.inference_state.goto(
            self.parent_context, self.tree_name, **kwargs
        )

    def is_import(self):
        imp = search_ancestor(self.tree_name, 'import_from', 'import_name')
        return imp is not None

    @property
    def string_name(self):
        return self.tree_name.value

    @property
    def start_pos(self):
        return self.tree_name.start_pos


class ValueNameMixin(object):
    def infer(self):
        return ValueSet([self._value])

    def _get_qualified_names(self):
        return self._value.get_qualified_names()

    def get_root_context(self):
        if self.parent_context is None:  # A module
            return self._value.as_context()
        return super(ValueNameMixin, self).get_root_context()

    @property
    def api_type(self):
        return self._value.api_type


class ValueName(ValueNameMixin, AbstractTreeName):
    def __init__(self, value, tree_name):
        super(ValueName, self).__init__(value.parent_context, tree_name)
        self._value = value

    def goto(self):
        return ValueSet([self._value.name])


class TreeNameDefinition(AbstractTreeName):
    _API_TYPES = dict(
        import_name='module',
        import_from='module',
        funcdef='function',
        param='param',
        classdef='class',
    )

    def infer(self):
        # Refactor this, should probably be here.
        from jedi.inference.syntax_tree import tree_name_to_values
        return tree_name_to_values(
            self.parent_context.inference_state,
            self.parent_context,
            self.tree_name
        )

    @property
    def api_type(self):
        definition = self.tree_name.get_definition(import_name_always=True)
        if definition is None:
            return 'statement'
        return self._API_TYPES.get(definition.type, 'statement')


class _ParamMixin(object):
    def maybe_positional_argument(self, include_star=True):
        options = [Parameter.POSITIONAL_ONLY, Parameter.POSITIONAL_OR_KEYWORD]
        if include_star:
            options.append(Parameter.VAR_POSITIONAL)
        return self.get_kind() in options

    def maybe_keyword_argument(self, include_stars=True):
        options = [Parameter.KEYWORD_ONLY, Parameter.POSITIONAL_OR_KEYWORD]
        if include_stars:
            options.append(Parameter.VAR_KEYWORD)
        return self.get_kind() in options

    def _kind_string(self):
        kind = self.get_kind()
        if kind == Parameter.VAR_POSITIONAL:  # *args
            return '*'
        if kind == Parameter.VAR_KEYWORD:  # **kwargs
            return '**'
        return ''


class ParamNameInterface(_ParamMixin):
    api_type = u'param'

    def get_kind(self):
        raise NotImplementedError

    def to_string(self):
        raise NotImplementedError

    def get_executed_param_name(self):
        """
        For dealing with type inference and working around the graph, we
        sometimes want to have the param name of the execution. This feels a
        bit strange and we might have to refactor at some point.

        For now however it exists to avoid infering params when we don't really
        need them (e.g. when we can just instead use annotations.
        """
        return None

    @property
    def star_count(self):
        kind = self.get_kind()
        if kind == Parameter.VAR_POSITIONAL:
            return 1
        if kind == Parameter.VAR_KEYWORD:
            return 2
        return 0


class BaseTreeParamName(ParamNameInterface, AbstractTreeName):
    annotation_node = None
    default_node = None

    def to_string(self):
        output = self._kind_string() + self.get_public_name()
        annotation = self.annotation_node
        default = self.default_node
        if annotation is not None:
            output += ': ' + annotation.get_code(include_prefix=False)
        if default is not None:
            output += '=' + default.get_code(include_prefix=False)
        return output

    def get_public_name(self):
        name = self.string_name
        if name.startswith('__'):
            # Params starting with __ are an equivalent to positional only
            # variables in typeshed.
            name = name[2:]
        return name

    def goto(self, **kwargs):
        return [self]


class SimpleParamName(BaseTreeParamName):
    def __init__(self, function_value, tree_name):
        super(BaseTreeParamName, self).__init__(
            function_value.get_default_param_context(), tree_name)
        self.function_value = function_value

    def _get_param_node(self):
        return search_ancestor(self.tree_name, 'param')

    @property
    def annotation_node(self):
        return self._get_param_node().annotation

    def infer_annotation(self, execute_annotation=True, ignore_stars=False):
        from jedi.inference.gradual.annotation import infer_param
        values = infer_param(
            self.function_value, self._get_param_node(),
            ignore_stars=ignore_stars)
        if execute_annotation:
            values = values.execute_annotation()
        return values

    def infer_default(self):
        node = self.default_node
        if node is None:
            return NO_VALUES
        return self.parent_context.infer_node(node)

    @property
    def default_node(self):
        return self._get_param_node().default

    def get_kind(self):
        tree_param = self._get_param_node()
        if tree_param.star_count == 1:  # *args
            return Parameter.VAR_POSITIONAL
        if tree_param.star_count == 2:  # **kwargs
            return Parameter.VAR_KEYWORD

        # Params starting with __ are an equivalent to positional only
        # variables in typeshed.
        if tree_param.name.value.startswith('__'):
            return Parameter.POSITIONAL_ONLY

        parent = tree_param.parent
        param_appeared = False
        for p in parent.children:
            if param_appeared:
                if p == '/':
                    return Parameter.POSITIONAL_ONLY
            else:
                if p == '*':
                    return Parameter.KEYWORD_ONLY
                if p.type == 'param':
                    if p.star_count:
                        return Parameter.KEYWORD_ONLY
                    if p == tree_param:
                        param_appeared = True
        return Parameter.POSITIONAL_OR_KEYWORD

    def infer(self):
        values = self.infer_annotation()
        if values:
            return values

        doc_params = docstrings.infer_param(self.function_value, self._get_param_node())
        return doc_params


class ParamName(SimpleParamName):
    def __init__(self, function_value, tree_name, arguments):
        super(ParamName, self).__init__(function_value, tree_name)
        self.arguments = arguments

    def infer(self):
        values = super(ParamName, self).infer()
        if values:
            return values

        return self.get_executed_param_name().infer()

    def get_executed_param_name(self):
        params_names, _ = self.arguments.get_executed_param_names_and_issues(self.function_value)
        return params_names[self._get_param_node().position_index]


class ParamNameWrapper(_ParamMixin):
    def __init__(self, param_name):
        self._wrapped_param_name = param_name

    def __getattr__(self, name):
        return getattr(self._wrapped_param_name, name)

    def __repr__(self):
        return '<%s: %s>' % (self.__class__.__name__, self._wrapped_param_name)


class ImportName(AbstractNameDefinition):
    start_pos = (1, 0)
    _level = 0

    def __init__(self, parent_context, string_name):
        self._from_module_context = parent_context
        self.string_name = string_name

    def get_qualified_names(self, include_module_names=False):
        if include_module_names:
            if self._level:
                assert self._level == 1, "Everything else is not supported for now"
                module_names = self._from_module_context.string_names
                if module_names is None:
                    return module_names
                return module_names + (self.string_name,)
            return (self.string_name,)
        return ()

    @property
    def parent_context(self):
        m = self._from_module_context
        import_values = self.infer()
        if not import_values:
            return m
        # It's almost always possible to find the import or to not find it. The
        # importing returns only one value, pretty much always.
        return next(iter(import_values))

    @memoize_method
    def infer(self):
        from jedi.inference.imports import Importer
        m = self._from_module_context
        return Importer(m.inference_state, [self.string_name], m, level=self._level).follow()

    def goto(self):
        return [m.name for m in self.infer()]

    @property
    def api_type(self):
        return 'module'


class SubModuleName(ImportName):
    _level = 1


class NameWrapper(object):
    def __init__(self, wrapped_name):
        self._wrapped_name = wrapped_name

    @abstractmethod
    def infer(self):
        raise NotImplementedError

    def __getattr__(self, name):
        return getattr(self._wrapped_name, name)

    def __repr__(self):
        return '%s(%s)' % (self.__class__.__name__, self._wrapped_name)
