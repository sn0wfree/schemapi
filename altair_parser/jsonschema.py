import jinja2
import os
from datetime import datetime

from . import trait_extractors as tx
from . import utils


OBJECT_TEMPLATE = '''
{%- if not hide_header -%}
# {{ cls.filename }}
# Auto-generated by altair_parser {{ date }}
{%- endif %}

{% if not hide_imports -%}
{%- for import in cls.object_imports %}
{{ import }}
{%- endfor %}
{%- endif %}

{% if cls.is_reference -%}
class {{ cls.classname }}({{ cls.wrapped_ref().classname }}):
    pass
{%- else -%}
class {{ cls.classname }}({{ cls.baseclass }}):
    """{{ cls.classname }} class

    Attributes
    ----------
    {%- for (name, prop) in cls.wrapped_properties().items() %}
    {{ name }} : {{ prop.type }}
        {{ prop.description }}
    {%- endfor %}
    """
    _default_trait = {{ cls.default_trait }}
    {%- for (name, prop) in cls.wrapped_properties().items() %}
    {{ name }} = {{ prop.trait_code }}
    {%- endfor %}
{%- endif %}
'''


class JSONSchema(object):
    """A class to wrap JSON Schema objects and reason about their contents"""
    object_template = OBJECT_TEMPLATE
    __draft__ = 4

    anonymous_objects = {}

    attr_defaults = {'title': '',
                     'description': '',
                     'properties': {},
                     'definitions': {},
                     'default': None,
                     'examples': {},
                     'type': 'object',
                     'required': [],
                     'additionalProperties': True}
    basic_imports = ["import traitlets as T",
                     "from . import jstraitlets as jst",
                     "from .baseobject import BaseObject"]
    # an ordered list of trait extractor classes.
    # these will be checked in-order, and return a trait_code when
    # a match is found.
    trait_extractors = [tx.RefTraitCode, tx.NotTraitCode, tx.AnyOfTraitCode,
                        tx.AllOfTraitCode, tx.OneOfTraitCode, tx.EnumTraitCode,
                        tx.SimpleTraitCode, tx.ArrayTraitCode,
                        tx.CompoundTraitCode, tx.ObjectTraitCode, ]

    def __init__(self, schema, context=None, parent=None, name=None, metadata=None):
        if not isinstance(schema, dict):
            raise ValueError("schema should be supplied as a dict")

        self.schema = schema
        self.parent = parent
        self.name = name
        self.metadata = metadata or {}

        # if context is not given, then assume this is a root instance that
        # defines its own context
        self.context = context or schema

    @classmethod
    def from_json_file(cls, filename):
        """Instantiate a JSONSchema object from a JSON file"""
        import json
        with open(filename) as f:
            schema = json.load(f)
        return cls(schema)

    def __getitem__(self, key):
        return self.schema[key]

    def __contains__(self, key):
        return key in self.schema

    def __getattr__(self, attr):
        if attr in self.attr_defaults:
            return self.schema.get(attr, self.attr_defaults[attr])
        raise AttributeError(f"'{self.__class__.__name__}' object "
                             f"has no attribute '{attr}'")

    def _new_anonymous_name(self):
        return "AnonymousMapping{0}".format(len(self.anonymous_objects) + 1)

    def copy(self, **kwargs):
        """Make a copy, optionally overwriting any init arguments"""
        kwds = dict(schema=self.schema, context=self.context,
                    parent=self.parent, name=self.name,
                    metadata=self.metadata)
        kwds.update(kwargs)
        return self.__class__(**kwds)

    def make_child(self, schema, name=None, metadata=None):
        """
        Make a child instance, appropriately defining the parent and context
        """
        return self.__class__(schema, context=self.context,
                              parent=self, name=name, metadata=metadata)

    @property
    def is_root(self):
        return self.context is self.schema

    @property
    def is_trait(self):
        return self.type != 'object' and not self.is_reference

    @property
    def is_object(self):
        return self.type == 'object' and not self.is_reference

    @property
    def is_reference(self):
        return '$ref' in self.schema

    @property
    def classname(self):
        if self.name:
            return utils.regularize_name(self.name)
        elif self.is_root:
            return "RootInstance"
        elif self.is_reference:
            return utils.regularize_name(self.schema['$ref'].split('/')[-1])
        elif self.is_object:
            hashval = self.schema_hash
            if hashval not in self.anonymous_objects:
                self.anonymous_objects[hashval] = {
                    'name': self._new_anonymous_name(),
                    'schema': self
                }
            return utils.regularize_name(self.anonymous_objects[hashval]['name'])
        else:
            raise NotImplementedError("class name for schema with keys "
                                      "{0}".format(tuple(self.schema.keys())))

    @property
    def schema_hash(self):
        return utils.hash_schema(self.schema)

    @property
    def modulename(self):
        return self.classname.lower()

    @property
    def filename(self):
        return self.modulename + '.py'

    @property
    def baseclass(self):
        return "BaseObject"

    @property
    def default_trait(self):
        if self.additionalProperties in [True, False]:
            return repr(self.additionalProperties)
        else:
            trait = self.make_child(self.additionalProperties)
            return "jst.DefaultTrait({0})".format(trait.trait_code)

    @property
    def import_statement(self):
        return f"from .{self.modulename} import {self.classname}"

    def wrapped_definitions(self):
        """Return definition dictionary wrapped as JSONSchema objects"""
        return {name.lower(): self.make_child(schema, name=name)
                for name, schema in self.definitions.items()}

    def wrapped_properties(self):
        """Return property dictionary wrapped as JSONSchema objects"""
        return {name: self.make_child(val, metadata={'required': name in self.required})
                for name, val in self.properties.items()}

    def wrapped_ref(self):
        return self.get_reference(self.schema['$ref'])

    def get_reference(self, ref):
        """
        Get the JSONSchema object for the given reference code.

        Reference codes should look something like "#/definitions/MyDefinition"
        """
        path = ref.split('/')
        name = path[-1]
        if path[0] != '#':
            raise ValueError(f"Unrecognized $ref format: '{ref}'")
        try:
            schema = self.context
            for key in path[1:]:
                schema = schema[key]
        except KeyError:
            raise ValueError(f"$ref='{ref}' not present in the schema")

        return self.make_child(schema, name=name)

    @property
    def trait_code(self):
        """Create the trait code for the given schema"""
        if self.metadata.get('required', False):
            kwargs = {'allow_undefined': False}
        else:
            kwargs = {}

        # TODO: handle multiple matches with an AllOf()
        for TraitExtractor in self.trait_extractors:
            trait_extractor = TraitExtractor(self)
            if trait_extractor.check():
                return trait_extractor.trait_code(**kwargs)
        else:
            raise ValueError("No recognized trait code for schema with "
                             "keys {0}".format(tuple(self.schema.keys())))

    def object_code(self, **kwargs):
        """Return code to define a traitlets.HasTraits object for this schema"""
        if self.is_reference or self.is_object:
            template = jinja2.Template(self.object_template)
        else:
            raise ValueError("Cannot generate object code for non-object")
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        return template.render(cls=self, date=now, **kwargs)

    @property
    def trait_imports(self):
        """Return the list of imports required in the trait_code definition"""
        # TODO: handle multiple matches with an AllOf()
        for TraitExtractor in self.trait_extractors:
            trait_extractor = TraitExtractor(self)
            if trait_extractor.check():
                return trait_extractor.imports()
        else:
            raise ValueError("No recognized trait code for schema with "
                             "keys {0}".format(tuple(self.schema.keys())))

    @property
    def object_imports(self):
        """Return the list of imports required in the object_code definition"""
        imports = list(self.basic_imports)
        if isinstance(self.additionalProperties, dict):
            default = self.make_child(self.additionalProperties)
            imports.extend(default.trait_imports)
        if self.is_reference:
            imports.append(self.wrapped_ref().import_statement)
        for trait in self.wrapped_properties().values():
            imports.extend(trait.trait_imports)
        return imports

    @property
    def module_imports(self):
        """List of imports of all definitions for the root module"""
        imports = []
        for obj in self.wrapped_definitions().values():
            if obj.is_object:
                imports.append(obj.import_statement)
        return imports

    def source_tree(self):
        """Return the JSON specification of the module source tree

        This can be passed to ``altair_parser.utils.load_dynamic_module``
        or to ``altair_parser.utils.save_module``
        """
        assert self.is_root
        submodroot = self.classname.lower()

        modspec = {
            'jstraitlets.py': open(os.path.join(os.path.dirname(__file__),
                                   'src', 'jstraitlets.py')).read(),
            'baseobject.py': open(os.path.join(os.path.dirname(__file__),
                                  'src', 'baseobject.py')).read(),
            self.filename: self.object_code()
        }

        modspec['__init__.py'] = '\n'.join([self.import_statement]
                                            + self.module_imports)

        modspec.update({schema.filename: schema.object_code()
                        for schema in self.wrapped_definitions().values()
                        if schema.is_object})

        return modspec
