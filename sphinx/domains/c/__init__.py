"""The C language domain."""

from __future__ import annotations

from types import NoneType
from typing import TYPE_CHECKING

from docutils import nodes
from docutils.parsers.rst import directives

from sphinx import addnodes
from sphinx.directives import ObjectDescription
from sphinx.domains import Domain, ObjType
from sphinx.domains.c._ast import (
    ASTDeclaration,
    ASTIdentifier,
    ASTNestedName,
)
from sphinx.domains.c._ids import _macro_keywords, _max_id
from sphinx.domains.c._parser import DefinitionParser
from sphinx.domains.c._symbol import Symbol, _DuplicateSymbolError
from sphinx.locale import _, __
from sphinx.roles import XRefRole
from sphinx.transforms import SphinxTransform
from sphinx.transforms.post_transforms import ReferencesResolver
from sphinx.util import logging
from sphinx.util.cfamily import (
    DefinitionError,
    NoOldIdError,
    anon_identifier_re,
)
from sphinx.util.docfields import Field, GroupedField, TypedField
from sphinx.util.docutils import SphinxDirective, SphinxRole
from sphinx.util.nodes import make_refnode

if TYPE_CHECKING:
    from collections.abc import Iterator, Set
    from typing import Any, ClassVar

    from docutils.nodes import Element, Node, TextElement, system_message

    from sphinx.addnodes import desc_signature, pending_xref
    from sphinx.application import Sphinx
    from sphinx.builders import Builder
    from sphinx.domains.c._symbol import LookupKey
    from sphinx.environment import BuildEnvironment
    from sphinx.util.typing import ExtensionMetadata, OptionSpec

# re-export objects for backwards compatibility
# See: https://github.com/sphinx-doc/sphinx/issues/12295
from sphinx.domains.c._ast import (  # NoQA: F401
    ASTAlignofExpr,
    ASTArray,
    ASTAssignmentExpr,
    ASTBase,
    ASTBinOpExpr,
    ASTBooleanLiteral,
    ASTBracedInitList,
    ASTCastExpr,
    ASTCharLiteral,
    ASTDeclarator,
    ASTDeclaratorNameBitField,
    ASTDeclaratorNameParam,
    ASTDeclaratorParen,
    ASTDeclaratorPtr,
    ASTDeclSpecs,
    ASTDeclSpecsSimple,
    ASTEnum,
    ASTEnumerator,
    ASTExpression,
    ASTFallbackExpr,
    ASTFunctionParameter,
    ASTIdExpression,
    ASTInitializer,
    ASTLiteral,
    ASTMacro,
    ASTMacroParameter,
    ASTNumberLiteral,
    ASTParameters,
    ASTParenExpr,
    ASTParenExprList,
    ASTPostfixArray,
    ASTPostfixCallExpr,
    ASTPostfixDec,
    ASTPostfixExpr,
    ASTPostfixInc,
    ASTPostfixMemberOfPointer,
    ASTPostfixOp,
    ASTSizeofExpr,
    ASTSizeofType,
    ASTStringLiteral,
    ASTStruct,
    ASTTrailingTypeSpec,
    ASTTrailingTypeSpecFundamental,
    ASTTrailingTypeSpecName,
    ASTType,
    ASTTypeWithInit,
    ASTUnaryOpExpr,
    ASTUnion,
)

logger = logging.getLogger(__name__)


def _make_phony_error_name() -> ASTNestedName:
    return ASTNestedName([ASTIdentifier('PhonyNameDueToError')], rooted=False)


class CObject(ObjectDescription[ASTDeclaration]):
    """Description of a C language object."""

    option_spec: ClassVar[OptionSpec] = {
        'no-index-entry': directives.flag,
        'no-contents-entry': directives.flag,
        'no-typesetting': directives.flag,
        'noindexentry': directives.flag,
        'nocontentsentry': directives.flag,
        'single-line-parameter-list': directives.flag,
    }

    def _add_enumerator_to_parent(self, ast: ASTDeclaration) -> None:
        assert ast.objectType == 'enumerator'
        # find the parent, if it exists && is an enum
        #                  then add the name to the parent scope
        symbol = ast.symbol
        assert symbol
        assert symbol.ident is not None
        parent_symbol = symbol.parent
        assert parent_symbol
        if parent_symbol.parent is None:
            # TODO: we could warn, but it is somewhat equivalent to
            # enumeratorss, without the enum
            return  # no parent
        parent_decl = parent_symbol.declaration
        if parent_decl is None:
            # the parent is not explicitly declared
            # TODO: we could warn, but?
            return
        if parent_decl.objectType != 'enum':
            # TODO: maybe issue a warning, enumerators in non-enums is weird,
            # but it is somewhat equivalent to enumeratorss, without the enum
            return
        if parent_decl.directiveType != 'enum':
            return

        target_symbol = parent_symbol.parent
        s = target_symbol.find_identifier(
            symbol.ident, matchSelf=False, recurseInAnon=True, searchInSiblings=False
        )
        if s is not None:
            # something is already declared with that name
            return
        decl_clone = symbol.declaration.clone()
        decl_clone.enumeratorScopedSymbol = symbol
        Symbol(
            parent=target_symbol,
            ident=symbol.ident,
            declaration=decl_clone,
            docname=self.env.current_document.docname,
            line=self.get_source_info()[1],
        )

    def add_target_and_index(
        self, ast: ASTDeclaration, sig: str, signode: TextElement
    ) -> None:
        ids = []
        for i in range(1, _max_id + 1):
            try:
                id = ast.get_id(version=i)
                ids.append(id)
            except NoOldIdError:
                assert i < _max_id
        # let's keep the newest first
        ids.reverse()
        newest_id = ids[0]
        assert newest_id  # shouldn't be None

        name = ast.symbol.get_full_nested_name().get_display_string().lstrip('.')
        if newest_id not in self.state.document.ids:
            # always add the newest id
            assert newest_id
            signode['ids'].append(newest_id)
            # only add compatibility ids when there are no conflicts
            for id in ids[1:]:
                if not id:  # is None when the element didn't exist in that version
                    continue
                if id not in self.state.document.ids:
                    signode['ids'].append(id)

            self.state.document.note_explicit_target(signode)

        if 'no-index-entry' not in self.options:
            index_text = self.get_index_text(name)
            self.indexnode['entries'].append((
                'single',
                index_text,
                newest_id,
                '',
                None,
            ))

    @property
    def object_type(self) -> str:
        raise NotImplementedError

    @property
    def display_object_type(self) -> str:
        return self.object_type

    def get_index_text(self, name: str) -> str:
        return _('%s (C %s)') % (name, self.display_object_type)

    def parse_definition(self, parser: DefinitionParser) -> ASTDeclaration:
        return parser.parse_declaration(self.object_type, self.objtype)

    def describe_signature(
        self, signode: TextElement, ast: ASTDeclaration, options: dict[str, Any]
    ) -> None:
        ast.describe_signature(signode, 'lastIsName', self.env, options)

    def run(self) -> list[Node]:
        env = self.env
        if env.current_document.c_parent_symbol is None:
            root = env.domaindata['c']['root_symbol']
            env.current_document.c_parent_symbol = root
            env.ref_context['c:parent_key'] = root.get_lookup_key()

        # When multiple declarations are made in the same directive
        # they need to know about each other to provide symbol lookup for function parameters.
        # We use last_symbol to store the latest added declaration in a directive.
        env.current_document.c_last_symbol = None
        return super().run()

    def handle_signature(self, sig: str, signode: TextElement) -> ASTDeclaration:
        parent_symbol: Symbol = self.env.current_document.c_parent_symbol

        max_len = (
            self.config.c_maximum_signature_line_length
            or self.config.maximum_signature_line_length
            or 0
        )
        signode['multi_line_parameter_list'] = (
            'single-line-parameter-list' not in self.options
            and (len(sig) > max_len > 0)
        )

        parser = DefinitionParser(sig, location=signode, config=self.config)
        try:
            ast = self.parse_definition(parser)
            parser.assert_end()
        except DefinitionError as e:
            logger.warning(e, location=signode)
            # It is easier to assume some phony name than handling the error in
            # the possibly inner declarations.
            name = _make_phony_error_name()
            symbol = parent_symbol.add_name(name)
            self.env.current_document.c_last_symbol = symbol
            raise ValueError from e

        try:
            symbol = parent_symbol.add_declaration(
                ast,
                docname=self.env.current_document.docname,
                line=self.get_source_info()[1],
            )
            # append the new declaration to the sibling list
            assert symbol.siblingAbove is None
            assert symbol.siblingBelow is None
            symbol.siblingAbove = self.env.current_document.c_last_symbol
            if symbol.siblingAbove is not None:
                assert symbol.siblingAbove.siblingBelow is None
                symbol.siblingAbove.siblingBelow = symbol
            self.env.current_document.c_last_symbol = symbol
        except _DuplicateSymbolError as e:
            # Assume we are actually in the old symbol,
            # instead of the newly created duplicate.
            self.env.current_document.c_last_symbol = e.symbol
            msg = __(
                'Duplicate C declaration, also defined at %s:%s.\n'
                "Declaration is '.. c:%s:: %s'."
            )
            logger.warning(
                msg,
                e.symbol.docname,
                e.symbol.line,
                self.display_object_type,
                sig,
                location=signode,
                type='duplicate_declaration',
                subtype='c',
            )

        if ast.objectType == 'enumerator':
            self._add_enumerator_to_parent(ast)

        # note: handle_signature may be called multiple time per directive,
        # if it has multiple signatures, so don't mess with the original options.
        options = dict(self.options)
        self.describe_signature(signode, ast, options)
        return ast

    def before_content(self) -> None:
        last_symbol: Symbol = self.env.current_document.c_last_symbol
        assert last_symbol
        self.oldParentSymbol = self.env.current_document.c_parent_symbol
        self.oldParentKey: LookupKey = self.env.ref_context['c:parent_key']
        self.env.current_document.c_parent_symbol = last_symbol
        self.env.ref_context['c:parent_key'] = last_symbol.get_lookup_key()

    def after_content(self) -> None:
        self.env.current_document.c_parent_symbol = self.oldParentSymbol
        self.env.ref_context['c:parent_key'] = self.oldParentKey

    def _object_hierarchy_parts(self, sig_node: desc_signature) -> tuple[str, ...]:
        last_symbol: Symbol = self.env.current_document.c_last_symbol
        return tuple(map(str, last_symbol.get_full_nested_name().names))

    def _toc_entry_name(self, sig_node: desc_signature) -> str:
        if not sig_node.get('_toc_parts'):
            return ''

        config = self.config
        objtype = sig_node.parent.get('objtype')
        if config.add_function_parentheses and (
            objtype in {'function', 'method'}
            or (objtype == 'macro' and '(' in sig_node.rawsource)
        ):
            parens = '()'
        else:
            parens = ''
        *parents, name = sig_node['_toc_parts']
        if config.toc_object_entries_show_parents == 'domain':
            return '::'.join((name + parens,))
        if config.toc_object_entries_show_parents == 'hide':
            return name + parens
        if config.toc_object_entries_show_parents == 'all':
            return '::'.join([*parents, name + parens])
        return ''


class CMemberObject(CObject):
    object_type = 'member'

    @property
    def display_object_type(self) -> str:
        # the distinction between var and member is only cosmetic
        assert self.objtype in {'member', 'var'}
        return self.objtype


_function_doc_field_types = [
    TypedField(
        'parameter',
        label=_('Parameters'),
        names=('param', 'parameter', 'arg', 'argument'),
        typerolename='expr',
        typenames=('type',),
    ),
    GroupedField(
        'retval',
        label=_('Return values'),
        names=('retvals', 'retval'),
        can_collapse=True,
    ),
    Field(
        'returnvalue',
        label=_('Returns'),
        has_arg=False,
        names=('returns', 'return'),
    ),
    Field(
        'returntype',
        label=_('Return type'),
        has_arg=False,
        names=('rtype',),
    ),
]


class CFunctionObject(CObject):
    object_type = 'function'

    doc_field_types = _function_doc_field_types.copy()


class CMacroObject(CObject):
    object_type = 'macro'

    doc_field_types = _function_doc_field_types.copy()


class CStructObject(CObject):
    object_type = 'struct'


class CUnionObject(CObject):
    object_type = 'union'


class CEnumObject(CObject):
    object_type = 'enum'


class CEnumeratorObject(CObject):
    object_type = 'enumerator'


class CTypeObject(CObject):
    object_type = 'type'


class CNamespaceObject(SphinxDirective):
    """This directive is just to tell Sphinx that we're documenting stuff in
    namespace foo.
    """

    has_content = False
    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec: ClassVar[OptionSpec] = {}

    def run(self) -> list[Node]:
        root_symbol = self.env.domaindata['c']['root_symbol']
        if self.arguments[0].strip() in {'NULL', '0', 'nullptr'}:
            symbol = root_symbol
            stack: list[Symbol] = []
        else:
            parser = DefinitionParser(
                self.arguments[0], location=self.get_location(), config=self.config
            )
            try:
                name = parser.parse_namespace_object()
                parser.assert_end()
            except DefinitionError as e:
                logger.warning(e, location=self.get_location())
                name = _make_phony_error_name()
            symbol = root_symbol.add_name(name)
            stack = [symbol]
        self.env.current_document.c_parent_symbol = symbol
        self.env.current_document.c_namespace_stack = stack
        self.env.ref_context['c:parent_key'] = symbol.get_lookup_key()
        return []


class CNamespacePushObject(SphinxDirective):
    has_content = False
    required_arguments = 1
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec: ClassVar[OptionSpec] = {}

    def run(self) -> list[Node]:
        if self.arguments[0].strip() in {'NULL', '0', 'nullptr'}:
            return []
        parser = DefinitionParser(
            self.arguments[0], location=self.get_location(), config=self.config
        )
        try:
            name = parser.parse_namespace_object()
            parser.assert_end()
        except DefinitionError as e:
            logger.warning(e, location=self.get_location())
            name = _make_phony_error_name()
        old_parent = self.env.current_document.c_parent_symbol
        if not old_parent:
            old_parent = self.env.domaindata['c']['root_symbol']
        symbol = old_parent.add_name(name)
        self.env.current_document.c_namespace_stack.append(symbol)
        self.env.current_document.c_parent_symbol = symbol
        self.env.ref_context['c:parent_key'] = symbol.get_lookup_key()
        return []


class CNamespacePopObject(SphinxDirective):
    has_content = False
    required_arguments = 0
    optional_arguments = 0
    final_argument_whitespace = True
    option_spec: ClassVar[OptionSpec] = {}

    def run(self) -> list[Node]:
        stack = self.env.current_document.c_namespace_stack
        if len(stack) == 0:
            logger.warning(
                'C namespace pop on empty stack. Defaulting to global scope.',
                location=self.get_location(),
            )
        else:
            stack.pop()
        if len(stack) > 0:
            symbol = stack[-1]
        else:
            symbol = self.env.domaindata['c']['root_symbol']
        self.env.current_document.c_parent_symbol = symbol
        self.env.ref_context['c:parent_key'] = symbol.get_lookup_key()
        return []


class AliasNode(nodes.Element):
    def __init__(
        self,
        sig: str,
        aliasOptions: dict[str, bool],
        document: Any,
        env: BuildEnvironment | None = None,
        parentKey: LookupKey | None = None,
    ) -> None:
        super().__init__()
        self.sig = sig
        self.aliasOptions = aliasOptions
        self.document = document
        if env is not None:
            if env.current_document.c_parent_symbol is None:
                root = env.domaindata['c']['root_symbol']
                env.current_document.c_parent_symbol = root
                env.ref_context['c:parent_key'] = root.get_lookup_key()
            self.parentKey = env.ref_context['c:parent_key']
        else:
            assert parentKey is not None
            self.parentKey = parentKey

    def copy(self) -> AliasNode:
        return self.__class__(
            self.sig,
            self.aliasOptions,
            self.document,
            env=None,
            parentKey=self.parentKey,
        )


class AliasTransform(SphinxTransform):
    default_priority = ReferencesResolver.default_priority - 1

    def _render_symbol(
        self,
        s: Symbol,
        maxdepth: int,
        skip_this: bool,
        alias_options: dict[str, bool],
        render_options: dict[str, bool],
        document: Any,
    ) -> list[Node]:
        if maxdepth == 0:
            recurse = True
        elif maxdepth == 1:
            recurse = False
        else:
            maxdepth -= 1
            recurse = True

        nodes: list[Node] = []
        if not skip_this:
            signode = addnodes.desc_signature('', '')
            nodes.append(signode)
            s.declaration.describe_signature(
                signode, 'markName', self.env, render_options
            )

        if recurse:
            if skip_this:
                child_container: list[Node] | addnodes.desc = nodes
            else:
                content = addnodes.desc_content()
                desc = addnodes.desc()
                content.append(desc)
                desc.document = document
                desc['domain'] = 'c'
                # 'desctype' is a backwards compatible attribute
                desc['objtype'] = desc['desctype'] = 'alias'
                desc['no-index'] = True
                child_container = desc

            for s_child in s.children:
                if s_child.declaration is None:
                    continue
                child_nodes = self._render_symbol(
                    s_child,
                    maxdepth=maxdepth,
                    skip_this=False,
                    alias_options=alias_options,
                    render_options=render_options,
                    document=document,
                )
                child_container.extend(child_nodes)

            if not skip_this and len(desc.children) != 0:
                nodes.append(content)
        return nodes

    def apply(self, **kwargs: Any) -> None:
        for node in self.document.findall(AliasNode):
            sig = node.sig
            parent_key = node.parentKey
            try:
                parser = DefinitionParser(sig, location=node, config=self.config)
                name = parser.parse_xref_object()
            except DefinitionError as e:
                logger.warning(e, location=node)
                name = None

            if name is None:
                # could not be parsed, so stop here
                signode = addnodes.desc_signature(sig, '')
                signode.clear()
                signode += addnodes.desc_name(sig, sig)
                node.replace_self(signode)
                continue

            root_symbol: Symbol = self.env.domains.c_domain.data['root_symbol']
            parent_symbol: Symbol | None = root_symbol.direct_lookup(parent_key)
            if not parent_symbol:
                logger.debug('Target: %s', sig)
                logger.debug('ParentKey: %s', parent_key)
                logger.debug(root_symbol.dump(1))
            assert parent_symbol  # should be there

            s = parent_symbol.find_declaration(
                name, 'any', matchSelf=True, recurseInAnon=True
            )
            if s is None:
                signode = addnodes.desc_signature(sig, '')
                node.append(signode)
                signode.clear()
                signode += addnodes.desc_name(sig, sig)

                logger.warning(
                    "Could not find C declaration for alias '%s'.", name, location=node
                )
                node.replace_self(signode)
                continue
            # Declarations like .. var:: int Missing::var
            # may introduce symbols without declarations.
            # But if we skip the root then it is ok to start recursion from it.
            if not node.aliasOptions['noroot'] and s.declaration is None:
                signode = addnodes.desc_signature(sig, '')
                node.append(signode)
                signode.clear()
                signode += addnodes.desc_name(sig, sig)

                logger.warning(
                    "Can not render C declaration for alias '%s'. No such declaration.",
                    name,
                    location=node,
                )
                node.replace_self(signode)
                continue

            nodes = self._render_symbol(
                s,
                maxdepth=node.aliasOptions['maxdepth'],
                skip_this=node.aliasOptions['noroot'],
                alias_options=node.aliasOptions,
                render_options={},
                document=node.document,
            )
            node.replace_self(nodes)


class CAliasObject(ObjectDescription[str]):
    option_spec: ClassVar[OptionSpec] = {
        'maxdepth': directives.nonnegative_int,
        'noroot': directives.flag,
    }

    def run(self) -> list[Node]:
        """On purpose this doesn't call the ObjectDescription version, but is based on it.
        Each alias signature may expand into multiple real signatures if 'noroot'.
        The code is therefore based on the ObjectDescription version.
        """
        if ':' in self.name:
            self.domain, _, self.objtype = self.name.partition(':')
        else:
            self.domain, self.objtype = '', self.name

        node = addnodes.desc()
        node.document = self.state.document
        node['domain'] = self.domain
        # 'desctype' is a backwards compatible attribute
        node['objtype'] = node['desctype'] = self.objtype
        node['no-index'] = True

        self.names: list[str] = []
        alias_options = {
            'maxdepth': self.options.get('maxdepth', 1),
            'noroot': 'noroot' in self.options,
        }
        if alias_options['noroot'] and alias_options['maxdepth'] == 1:
            logger.warning(
                'Error in C alias declaration.'
                " Requested 'noroot' but 'maxdepth' 1."
                ' When skipping the root declaration,'
                " need 'maxdepth' 0 for infinite or at least 2.",
                location=self.get_location(),
            )
        for sig in self.get_signatures():
            node.append(
                AliasNode(sig, alias_options, self.state.document, env=self.env)
            )
        return [node]


class CXRefRole(XRefRole):
    def process_link(
        self,
        env: BuildEnvironment,
        refnode: Element,
        has_explicit_title: bool,
        title: str,
        target: str,
    ) -> tuple[str, str]:
        refnode.attributes.update(env.ref_context)

        if not has_explicit_title:
            # major hax: replace anon names via simple string manipulation.
            # Can this actually fail?
            title = anon_identifier_re.sub('[anonymous]', str(title))

        if not has_explicit_title:
            target = target.lstrip('~')  # only has a meaning for the title
            # if the first character is a tilde, don't display the module/class
            # parts of the contents
            if title[0:1] == '~':
                title = title[1:]
                dot = title.rfind('.')
                if dot != -1:
                    title = title[dot + 1 :]
        return title, target


class CExprRole(SphinxRole):
    def __init__(self, asCode: bool) -> None:
        super().__init__()
        if asCode:
            # render the expression as inline code
            self.class_type = 'c-expr'
        else:
            # render the expression as inline text
            self.class_type = 'c-texpr'

    def run(self) -> tuple[list[Node], list[system_message]]:
        text = self.text.replace('\n', ' ')
        parser = DefinitionParser(
            text, location=self.get_location(), config=self.config
        )
        # attempt to mimic XRefRole classes, except that...
        try:
            ast = parser.parse_expression()
        except DefinitionError as ex:
            logger.warning(
                'Unparseable C expression: %r\n%s',
                text,
                ex,
                location=self.get_location(),
            )
            # see below
            node = addnodes.desc_inline('c', text, text, classes=[self.class_type])
            return [node], []
        parent_symbol = self.env.current_document.c_parent_symbol
        if parent_symbol is None:
            parent_symbol = self.env.domaindata['c']['root_symbol']
        # ...most if not all of these classes should really apply to the individual references,
        # not the container node
        signode = addnodes.desc_inline('c', classes=[self.class_type])
        ast.describe_signature(signode, 'markType', self.env, parent_symbol)
        return [signode], []


class CDomain(Domain):
    """C language domain."""

    name = 'c'
    label = 'C'
    object_types = {
        # 'identifier' is the one used for xrefs generated in signatures, not in roles
        'member': ObjType(_('member'), 'var', 'member', 'data', 'identifier'),
        'var': ObjType(_('variable'), 'var', 'member', 'data', 'identifier'),
        'function': ObjType(_('function'), 'func', 'identifier', 'type'),
        'macro': ObjType(_('macro'), 'macro', 'identifier'),
        'struct': ObjType(_('struct'), 'struct', 'identifier', 'type'),
        'union': ObjType(_('union'), 'union', 'identifier', 'type'),
        'enum': ObjType(_('enum'), 'enum', 'identifier', 'type'),
        'enumerator': ObjType(_('enumerator'), 'enumerator', 'identifier'),
        'type': ObjType(_('type'), 'identifier', 'type'),
        # generated object types
        'functionParam': ObjType(
            _('function parameter'), 'identifier', 'var', 'member', 'data'
        ),
    }

    directives = {
        'member': CMemberObject,
        'var': CMemberObject,
        'function': CFunctionObject,
        'macro': CMacroObject,
        'struct': CStructObject,
        'union': CUnionObject,
        'enum': CEnumObject,
        'enumerator': CEnumeratorObject,
        'type': CTypeObject,
        # scope control
        'namespace': CNamespaceObject,
        'namespace-push': CNamespacePushObject,
        'namespace-pop': CNamespacePopObject,
        # other
        'alias': CAliasObject,
    }
    roles = {
        'member': CXRefRole(),
        'data': CXRefRole(),
        'var': CXRefRole(),
        'func': CXRefRole(fix_parens=True),
        'macro': CXRefRole(),
        'struct': CXRefRole(),
        'union': CXRefRole(),
        'enum': CXRefRole(),
        'enumerator': CXRefRole(),
        'type': CXRefRole(),
        'expr': CExprRole(asCode=True),
        'texpr': CExprRole(asCode=False),
    }
    initial_data: ClassVar[dict[str, Symbol | dict[str, tuple[str, str, str]]]] = {
        'root_symbol': Symbol(None, None, None, None, None),
        'objects': {},  # fullname -> docname, node_id, objtype
    }

    def clear_doc(self, docname: str) -> None:
        if Symbol.debug_show_tree:
            logger.debug('clear_doc: %s', docname)
            logger.debug('\tbefore:')
            logger.debug(self.data['root_symbol'].dump(1))
            logger.debug('\tbefore end')

        root_symbol = self.data['root_symbol']
        root_symbol.clear_doc(docname)

        if Symbol.debug_show_tree:
            logger.debug('\tafter:')
            logger.debug(self.data['root_symbol'].dump(1))
            logger.debug('\tafter end')
            logger.debug('clear_doc end: %s', docname)

    def process_doc(
        self, env: BuildEnvironment, docname: str, document: nodes.document
    ) -> None:
        if Symbol.debug_show_tree:
            logger.debug('process_doc: %s', docname)
            logger.debug(self.data['root_symbol'].dump(0))
            logger.debug('process_doc end: %s', docname)

    def process_field_xref(self, pnode: pending_xref) -> None:
        pnode.attributes.update(self.env.ref_context)

    def merge_domaindata(self, docnames: Set[str], otherdata: dict[str, Any]) -> None:
        if Symbol.debug_show_tree:
            logger.debug('merge_domaindata:')
            logger.debug('\tself:')
            logger.debug(self.data['root_symbol'].dump(1))
            logger.debug('\tself end')
            logger.debug('\tother:')
            logger.debug(otherdata['root_symbol'].dump(1))
            logger.debug('\tother end')
            logger.debug('merge_domaindata end')

        self.data['root_symbol'].merge_with(
            otherdata['root_symbol'], docnames, self.env
        )
        our_objects = self.data['objects']
        for fullname, (fn, id_, objtype) in otherdata['objects'].items():
            if fn in docnames:
                if fullname not in our_objects:
                    our_objects[fullname] = (fn, id_, objtype)
                # no need to warn on duplicates, the symbol merge already does that

    def _resolve_xref_inner(
        self,
        env: BuildEnvironment,
        fromdocname: str,
        builder: Builder,
        typ: str,
        target: str,
        node: pending_xref,
        contnode: Element,
    ) -> tuple[nodes.reference, str] | tuple[None, None]:
        parser = DefinitionParser(target, location=node, config=env.config)
        try:
            name = parser.parse_xref_object()
        except DefinitionError as e:
            logger.warning(
                'Unparseable C cross-reference: %r\n%s', target, e, location=node
            )
            return None, None
        parent_key: LookupKey | None = node.get('c:parent_key', None)
        root_symbol = self.data['root_symbol']
        if parent_key:
            parent_symbol: Symbol = root_symbol.direct_lookup(parent_key)
            if not parent_symbol:
                logger.debug('Target: %s', target)
                logger.debug('ParentKey: %s', parent_key)
                logger.debug(root_symbol.dump(1))
            assert parent_symbol  # should be there
        else:
            parent_symbol = root_symbol
        s = parent_symbol.find_declaration(
            name, typ, matchSelf=True, recurseInAnon=True
        )
        if s is None or s.declaration is None:
            return None, None

        # TODO: check role type vs. object type

        declaration = s.declaration
        display_name = name.get_display_string()
        docname = s.docname
        assert docname

        return make_refnode(
            builder,
            fromdocname,
            docname,
            declaration.get_newest_id(),
            contnode,
            display_name,
        ), declaration.objectType

    def resolve_xref(
        self,
        env: BuildEnvironment,
        fromdocname: str,
        builder: Builder,
        typ: str,
        target: str,
        node: pending_xref,
        contnode: Element,
    ) -> nodes.reference | None:
        return self._resolve_xref_inner(
            env, fromdocname, builder, typ, target, node, contnode
        )[0]

    def resolve_any_xref(
        self,
        env: BuildEnvironment,
        fromdocname: str,
        builder: Builder,
        target: str,
        node: pending_xref,
        contnode: Element,
    ) -> list[tuple[str, nodes.reference]]:
        with logging.suppress_logging():
            retnode, objtype = self._resolve_xref_inner(
                env, fromdocname, builder, 'any', target, node, contnode
            )
        if retnode:
            return [('c:' + self.role_for_objtype(objtype), retnode)]
        return []

    def get_objects(self) -> Iterator[tuple[str, str, str, str, str, int]]:
        root_symbol = self.data['root_symbol']
        for symbol in root_symbol.get_all_symbols():
            if symbol.declaration is None:
                continue
            assert symbol.docname
            full_nested_name = symbol.get_full_nested_name()
            name = str(full_nested_name).lstrip('.')
            dispname = full_nested_name.get_display_string().lstrip('.')
            object_type = symbol.declaration.objectType
            docname = symbol.docname
            newest_id = symbol.declaration.get_newest_id()
            yield name, dispname, object_type, docname, newest_id, 1


def setup(app: Sphinx) -> ExtensionMetadata:
    app.add_domain(CDomain)
    app.add_config_value('c_id_attributes', [], 'env', types=frozenset({list, tuple}))
    app.add_config_value(
        'c_paren_attributes', [], 'env', types=frozenset({list, tuple})
    )
    app.add_config_value(
        'c_extra_keywords',
        _macro_keywords,
        'env',
        types=frozenset({frozenset, list, set, tuple}),
    )
    app.add_config_value(
        'c_maximum_signature_line_length',
        None,
        'env',
        types=frozenset({int, NoneType}),
    )
    app.add_post_transform(AliasTransform)

    return {
        'version': 'builtin',
        'env_version': 3,
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
