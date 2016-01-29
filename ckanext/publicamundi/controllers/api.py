import logging
import json
import time
import datetime

from itertools import ifilter, islice
from pylons import g, config

from ckan.lib.base import (BaseController, c, request, response, abort, redirect)
import ckan.model as model
import ckan.plugins.toolkit as toolkit
import ckan.logic as logic

from ckanext.publicamundi.lib.util import to_json
from ckanext.publicamundi.lib import uploader
from ckanext.publicamundi.lib import vocabularies

log = logging.getLogger(__name__)

_ = toolkit._
_url = toolkit.url_for
_get_action = toolkit.get_action
_check_access = toolkit.check_access

content_types = {
    'json': 'application/json; charset=utf8',
    'xml': 'application/xml; charset=utf8',
}

resource_formats = toolkit.aslist(config.get('ckanext.publicamundi.resource_formats'))

class Controller(BaseController):
    '''Publicamundi-specific api actions'''

    @staticmethod
    def _make_context(**opts):
        ctx = { 
            'model': model, 
            'session': model.Session, 
            'api_version': 3 
        }
        if opts:
            ctx.update(opts)
        return ctx
    
    #
    # Autocomplete helpers
    #

    def resource_mimetype_autocomplete(self):
        '''Return list of mime types whose names contain a string
        '''
        
        q = request.params.get('incomplete', '')
        q = str(q).lower()
        limit  = request.params.get('limit', 12)

        context = self._make_context()
        data_dict = { 'q': q, 'limit': limit }

        # Invoke the action we have registered via IActions 
        r = logic.get_action('mimetype_autocomplete')(context, data_dict)

        result_set = {
            'ResultSet': {
                'Result': [{'name': t } for t in r]
            }
        }

        response.headers['Content-Type'] = content_types['json']
        return [to_json(result_set)]

    def resource_format_autocomplete(self):
        '''Return list of resource formats whose names contain a string

        Note: Maybe, should be changed to match only at the beginning?
        '''
         
        q = request.params.get('incomplete', '')
        q = str(q).lower()
        limit  = request.params.get('limit', 12)
        
        context = { 'model': model, 'session': model.Session }
        data_dict = { 'q': q, 'limit': limit }
        
        toolkit.check_access('site_read', context, data_dict)
        
        # The result will be calculated as the merge of matches from 2 sources:
        #  * a static list of application-domain formats supplied at configuration time 
        #  * a dynamic list of formats supplied for other resources: that's what CKAN's 
        #    action `format_autocomplete` already does.

        results = []
        
        r1 = logic.get_action('format_autocomplete')(context, data_dict)
        results.extend(({ 'name': t, 'text': t.upper() } for t in r1))

        limit -= len(results)
        r2 = ifilter(lambda t: (not t in r1) and (t.find(q) >= 0), resource_formats)
        r2 = islice(r2, 0, limit)
        results.extend(({ 'name': t, 'text': t.upper() } for t in r2))
   
        result_set = { 'ResultSet': { 'Result': results } } 
        response.headers['Content-Type'] = content_types['json']
        return [to_json(result_set)]
    
    #
    # Vocabularies
    #

    # Note: The 1st thought was to rely on CKAN's vocabulary/tag functionality.
    # But because (e.g. for INSPIRE-related thesauri) we need to distinguish 
    # between the human and the machine-friendly view of a term, we had to use
    # our own vocabularies. So, provided that we had some way to solve this,
    # the following api calls wont be needed any more.

    def vocabularies_list(self):
        response.headers['Content-Type'] = content_types['json']
        return [json.dumps(vocabularies.get_names())]

    def vocabulary_get(self, name):
        name = str(name)
        r = None
        
        vocab = vocabularies.by_name(name) 
        if vocab:
            r = {
                'date_type': vocab.get('date_type'),
                'reference_date': vocab.get('reference_date'),
                'title': vocab.get('title'),
                'name': vocab.get('name'),
                'terms': [{'token': t.token, 'value': t.value, 'title': t.title} 
                    for t in vocab['vocabulary']],
            }
        
        response.headers['Content-Type'] = content_types['json']
        return [to_json(r)]
    
    #
    # Datasets
    #

    def dataset_export(self, name_or_id):
        
        context = self._make_context() 
        try:
            result = _get_action('dataset_export')(context, { 'id': name_or_id })
        except toolkit.ObjectNotFound as ex:
            abort(404, detail=u'Package "%s" not found' % (name_or_id)) 
        except toolkit.NotAuthorized as ex:
            abort(401, detail=u'Not authorized to export package "%s"' % (name_or_id))

        exported_url = result.get('url')
        redirect(exported_url)
        return
 
    def dataset_export_dcat(self, name_or_id):
        
        context = self._make_context() 
        try:
            result = _get_action('dataset_export_dcat')(context, { 'id': name_or_id })
        except toolkit.ObjectNotFound as ex:
            abort(404, detail=u'Package "%s" not found' % (name_or_id)) 
        except toolkit.NotAuthorized as ex:
            abort(401, detail=u'Not authorized to export package "%s"' % (name_or_id))

        exported_url = result.get('url')
        redirect(exported_url)
        return

    def dataset_import(self):
        
        post = request.params

        # Forward to the dataset_import action

        context = self._make_context()
        data_dict = {
            'source': post.get('source'),
            'dtype': post.get('dtype'),
            'owner_org': post.get('owner_org'),
        }
        result = _get_action('dataset_import')(context, data_dict)
        return result

