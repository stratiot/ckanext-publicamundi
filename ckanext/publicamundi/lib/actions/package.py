# -*- coding: utf-8 -*-

import os
import errno
import fcntl
import logging
import datetime
import requests
import urlparse
import pylons
from operator import itemgetter, attrgetter

import ckan.model as model
import ckan.logic as logic
import ckan.plugins.toolkit as toolkit
from ckan.lib.plugins import lookup_package_plugin
from ckan.lib.uploader import get_storage_path

from ckanext.publicamundi import reference_data
from ckanext.publicamundi.cache_manager import get_cache
from ckanext.publicamundi.lib.actions import (
    NameConflict, IdentifierConflict, Invalid)
from ckanext.publicamundi.lib.languages import check as check_language
from ckanext.publicamundi.lib.metadata import (
    Metadata, make_metadata, class_for_metadata,
    serializer_for, xml_serializer_for,
    fields, bound_field,
    translator_for)

log = logging.getLogger(__name__)

_ = toolkit._
_get_action = toolkit.get_action
_check_access = toolkit.check_access

## Action API ##

@logic.side_effect_free
def dataset_export(context, data_dict):
    '''Export a dataset to XML.
    
    :param id: the name or id of the dataset to be exported.
    :type id: string

    rtype: dict
    '''

    pkg = _get_action('package_show')(context, data_dict)
    
    dtype = pkg.get('dataset_type')
    obj = pkg.get(dtype) if dtype else None
    
    cached_metadata = get_cache('metadata') 
    result = None
    if obj:
        # Get a proper serializer
        xser = xml_serializer_for(obj)
        xser.target_namespace = pylons.config.get('ckan.site_url') 
        # Persist exported XML data and wrap into a URL
        name = '%(name)s@%(revision_id)s' % (pkg)
        cached = cached_metadata.get(name, createfunc=xser.dumps)
        link = toolkit.url_for(
            controller='ckanext.publicamundi.controllers.files:Controller',
            action='download_file', 
            object_type='metadata',
            name_or_id=name,
            filename=('%(name)s.xml' % (pkg)))
        result = dict(url=link)
    
    return result

@logic.side_effect_free
def dataset_export_dcat(context, data_dict):
    '''Export a dataset to RDF XML using GeoDCAT XSLT.

    :param id: the name or id of the dataset to be exported.
    :type id: string

    rtype: dict
    '''

    pkg = _get_action('package_show')(context, data_dict)
    
    dtype = pkg.get('dataset_type')
    obj = pkg.get(dtype) if dtype else None
    
    cached_metadata = get_cache('metadata')
    result = None
    if obj:
        # Get a proper serializer
        xser = xml_serializer_for(obj)
        xser.target_namespace = pylons.config.get('ckan.site_url')
        name = '%(name)s@%(revision_id)s.dcat' % (pkg)
        cached = cached_metadata.get(name, createfunc=lambda: _transform_dcat(xser.to_xml()))
        link = toolkit.url_for(
            controller='ckanext.publicamundi.controllers.files:Controller',
            action='download_file',
            object_type='metadata',
            name_or_id=name,
            filename=('%(name)s.xml' % (pkg)))
        result = dict(url=link)

    return result

def dataset_import(context, data_dict):
    '''Import a dataset from a given XML source.

    This action, depending also on the value of its flags, can raise one of:

      * actions.Invalid: received invalid input
      * actions.IdentifierConflict: a package with the same identifier already exists
      * actions.NameConflict: a package with the same name already exists
      * toolkit.ValidationError: validation fails while trying to create a package 

    :param source: This is either a string representing a (local or external) URL 
        or a file-like object.
    :type q: string or file-like
    
    :param dtype: the dataset-type i.e. the schema of imported metadata
    :type dtype: string

    :param owner_org: the machine-name for the owner organization 
    :type owner_org: string

    :param continue_on_errors: hint on what to do when validation fails
    :type continue_on_errors: boolean
    
    :param rename_if_conflict: hint on what to do when a name conflict is encountered
    :type rename_if_conflict: boolean

    :rtype: basic info for the newly created package 
    '''
      
    # Read parameters

    try:
        source = data_dict['source']
    except KeyError:
        raise Invalid({'source': 'The `source` parameter is required'})
    
    dtype = data_dict.get('dtype', 'inspire')

    try:
        owner_org = data_dict['owner_org']
    except KeyError:
        raise Invalid({'owner_org':
            'The `owner_org` parameter is required.\n'
            'Hint: Use `organization_list_for_user` to retrieve a valid list.'})
        
    allow_rename = data_dict.get('rename_if_conflict', False)
    allow_validation_errors = data_dict.get('continue_on_errors', False)

    # Fetch raw XML data
    
    xmldata = None
    
    if isinstance(source, basestring):
        # Assume source is a URL
        if not source.startswith('http://'):
            source = pylons.config['ckan.site_url'] + source.strip('/')
        source = urlparse.urlparse(source)
        r1 = requests.get(source.geturl())
        if not r1.ok:
            raise Invalid({'source': _('Cannot fetch metadata from source URL')})
        elif not r1.headers['content-type'] in ['application/xml', 'text/xml']:
            raise Invalid({'source': _('The source does not contain XML data')})
        else:
            xmldata = r1.content
    else:
        # Assume source is a file-like object
        try:
            xmldata = source.read()
        except:
            raise Invalid({'source': _('Cannot read from source')})

    # Parse XML data as metadata of `dtype` schema
    
    obj = make_metadata(dtype)
    try:
        obj = xml_serializer_for(obj).loads(xmldata)
    except AssertionError as ex:
        raise ex
    except Exception as ex:
        # Map all parse exceptions to Invalid
        log.info('Failed to parse XML metadata: %s', ex)
        raise Invalid({'source': _('The given XML file is malformed: %s') % (ex)})

    # Prepare package dict

    pkg_dict = {'version': '1.0'}
    pkg_dict.update(obj.deduce_fields())
    pkg_dict.update({ 
        'owner_org': owner_org,
        'type': 'dataset',
        'dataset_type': dtype,
        dtype: obj.to_dict(flat=False),
    })
    
    # If an identifier is passed, check that this is not already present.
    # Note This is no guarantee that the identifier will be available when
    # `package_create` is actually invoked.

    identifier = pkg_dict.get('id')
    if identifier and _check_package_id_exists(context, identifier):
        raise IdentifierConflict({
           'id':  _('A package identified as %s already exists') % (identifier)})
 
    # Find and assign a machine-name for this package
    # Note We just find the 1st available name. As noted before, this is no 
    # guarantee that will be available when `package_create` is invoked.
    
    basename = pkg_dict['name']
    max_num_probes = 10 if allow_rename else 1
    name = _find_a_package_name(context, basename, max_num_probes)
    if not name:
        raise NameConflict({
            'name': _('The package name %r is not available') % (basename)})
    else:
        pkg_dict['name'] = name
        pkg_dict['title'] += ' ' + name[len(basename):]
    
    # Create/Update package
    
    schema1, validation_errors, error_message = None, None, None
    
    if identifier:
        # Must override catalog-wide schema for actions in this context
        schema1 = lookup_package_plugin().create_package_schema()
        schema1['id'] = [unicode]
    
    ctx = _make_context(context)
    if schema1:
        ctx['schema'] = schema1
    
    try:
        pkg_dict = _get_action('package_create')(ctx, data_dict=pkg_dict)
    except toolkit.ValidationError as ex:
        if 'name' in ex.error_dict:
            # The name is probably taken, re-raise exception
            raise ex
        elif allow_validation_errors:
            # Save errors and retry with a different context
            validation_errors = ex.error_dict
            error_message = ex.message or _('The dataset contains invalid metadata')
            ctx = _make_context(context, skip_validation=True)
            if schema1:
                ctx['schema'] = schema1
            pkg_dict = _get_action('package_create')(ctx, data_dict=pkg_dict)
            log.warn('Forced to create an invalid package as %r ' % (name))
        else:
            raise ex

    assert name == pkg_dict['name']
    assert (not identifier) or (identifier == pkg_dict['id'])

    return {
        # Provide basic package fields
        'id': pkg_dict['id'], 
        'name': name,
        'title': pkg_dict['title'],
        'state': pkg_dict.get('state'),
        # Provide details on validation (meaningfull if allow_validation_errors)
        'validation': {
            'message': error_message,
            'errors': validation_errors,
        },
    }

def dataset_translation_update_field(context, data_dict):
    '''Translate a dataset field for the active language.

    This is similar to `dataset_translation_update` but only updates a field per
    call. It's purpose is to be used when fields are updated individually.

    :param id: the name or id of the package.
    :type id: string

    :param translate_to_language: the target language
    :type translate_to_language: string
     
    :param key: the field's key path (as dotted path or as a tuple)
    :type : string or tuple
    
    :param value: the translated text value
    :type value: string
    '''
    
    # Determine target language
    
    lang = _target_language(data_dict)

    # Fetch package in source language

    context.update({'translate': False})
    pkg = _get_action('package_show')(context, {'id': data_dict['id']})
    dtype = pkg['dataset_type']

    source_lang = pkg['language']
    if lang == source_lang:
        msg = 'The target language same as source language (%s)' % (lang)
        raise Invalid({'translate_to_language': msg})
    
    key = data_dict.get('key')
    if not key:
        raise Invalid({'key': 'Missing'})
    if isinstance(key, basestring):
        key = tuple(key.split('.'))
    else:
        key = tuple(key)

    value = data_dict.get('value')
    if not value:
        raise Invalid({'value': 'Missing'})
    value = unicode(value)

    # Check authorization

    _check_access(
        'package_translation_update', context, {'org': pkg['owner_org']})
    
    # Update translation for field

    md = pkg[dtype]
    translator = translator_for(md, source_lang)
    
    msg = None
    yf = None
    if len(key) < 2: 
        # Translate a top-level field
        if key[0] in ['title', 'notes']:
            # A core CKAN translatable field
            uf = fields.TextField() 
            yf = bound_field(uf, key, pkg[key[0]])
    else:
        # Translate a field from structured metadata
        if key[0] == dtype:
            try:
                yf = md.get_field(key[1:])
            except:
                yf = None
                msg = 'No such field'
            if yf and not yf.queryTaggedValue('translatable'):
                yf = None 
                msg = 'Not translatable'
            if yf:
                yf.context.key = key
    
    tr = None
    if yf and yf.context.value:
        tr = translator.get_field_translator(yf)
        if tr:
            tr.translate(lang, value)

    res = {'updated': bool(tr)}
    if msg:
        res['message'] = msg;

    return res

def dataset_translation_update(context, data_dict):
    '''Translate dataset for the active language.
    
    The accepted format data_dict is as the one passed to core `package_update`. 
    
    An additional parameter is `translate_to_language` which determines the target
    language. If not supplied, the active language (from Pylons request) will be used.
    
    All non-translatable fields will be ignored. 
    
    All fields that are not present (or are empty) in source package, will also be
    ignored.

    :param id: the name or id of the package.
    :type id: string
   
    :param translate_to_language: the target language
    :type translate_to_language: string
    
    rtype: dict
    '''
     
    # Determine target language
    
    lang = _target_language(data_dict)

    # Fetch package in source language

    context.update({
        'translate': False,
        'return_json': True
    })
    pkg = _get_action('package_show')(context, {'id': data_dict['id']})
    dtype = pkg['dataset_type']

    source_lang = pkg['language']
    if lang == source_lang:
        msg = 'The target language same as source language (%s)' % (lang)
        raise Invalid({'translate_to_language': msg})
 
    md = class_for_metadata(dtype)()
    md.from_json(pkg[dtype])
   
    # Check authorization

    _check_access(
        'package_translation_update', context, {'org': pkg['owner_org']})
    
    # Translate structured metadata
    
    translator = translator_for(md, source_lang)
    md = translator.translate(lang, data_dict[dtype])
    
    pkg[dtype] = md.to_json(return_string=False)

    # Translate core CKAN metadata

    field_translator = translator.get_field_translator
    uf = fields.TextField()
    for k in ('title', 'notes'):
        v = data_dict.get(k)
        if not (v and pkg.get(k)):
            continue # nothing to translate
        tr = field_translator(bound_field(uf, (k,), pkg[k]))
        if not tr:
            continue 
        yf = tr.translate(lang, v)
        pkg[k] = v

    # Return translated view of this package

    pkg['translated_to_language'] = lang
    return pkg

def dataset_translation_check_authorized(context, data_dict):
    '''Check if authorized to translate fields for a given package.
    '''
    userobj = context['auth_user_obj']

    # Todo Maybe more granular authorization (a translator role ?)
     
    q = {
        'id': data_dict['org'], 
        'object_type': 'user', 
    }

    q['capacity'] = 'admin'
    admins = map(itemgetter(0), _get_action('member_list')(context, q))
    if userobj.id in admins:
        return {'success': True}
    
    q['capacity'] = 'editor'
    editors = map(itemgetter(0), _get_action('member_list')(context, q))
    if userobj.id in editors:
        return {'success': True}

    return {'success': False, 'msg': _('Not an editor for this organization')}

## Helpers ##

def _target_language(data_dict):
    lang = data_dict.get('translate_to_language')
    if not lang:
        lang = pylons.i18n.get_lang()
        lang = lang[0] if lang else 'en'
    else:
        try:
            lang = check_language(lang)
        except ValueError:
            msg = 'Unknown target language (%s)' % (lang)
            raise Invalid({'translate_to_language': msg}) 
    return lang

def _make_context(context, **opts):
    '''Make a new context for an action, based on an initial context.
    
    This is needed in case we want to retry a same action, because re-using the
    previous context can lead to strange errors (updates instead of creates etc) 
    '''
    
    ctx = { 
        'model': context['model'], 
        'session': context['session'], 
        'user': toolkit.c.user,
    }
    
    if 'api_version' in context:
        ctx['api_version'] = context['api_version']

    ctx.update(opts)
    
    return ctx

def _check_package_id_exists(context, identifier):
    '''Check if a package with the given identifier already exists
    '''
    res = True
    ctx = _make_context(context, return_id_only=True)
    try:
         _get_action('package_show')(ctx, data_dict=dict(id=identifier))
    except toolkit.ObjectNotFound:
        res = False
    return res

def _find_a_package_name(context, basename, max_num_probes=12):
    '''Probe until you find an available (non-occupied) package name.
    
    The result name will be based on `name` and will try to append a suffix
    until it succeeds or until it reaches `max_num_probes`.

    If you pass 1 as value for `max_num_probes`, it will essentially test if the given
    name (unmodified) is available (and will also return it).
    '''
    
    suffix_fmt = '~{num_probes:d}'
   
    ctx = _make_context(context, return_id_only=True)
    name, num_probes, found, exhausted = basename, 0, False, False
    while not (found or exhausted):
        try:
            num_probes += 1
            _get_action('package_show')(ctx, data_dict=dict(id=name))
        except toolkit.ObjectNotFound:
            found = True
        else:
            if num_probes < max_num_probes:
                # Rename and retry
                name = basename + suffix_fmt.format(num_probes=num_probes) 
            else:
                exhausted = True
   
    return name if found else None

def _transform_dcat(xml_dom):
    from lxml import etree

    xsl_file = reference_data.get_path('xsl/iso-19139-to-dcat-ap.xsl')
    result = None
    with open(xsl_file, 'r') as fp:
        # Transform using XSLT
        dcat_xslt = etree.parse(fp)
        dcat_transform = etree.XSLT(dcat_xslt)
        result = dcat_transform(xml_dom)
        result = unicode(result).encode('utf-8')

    return result
