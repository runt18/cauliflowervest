#!/usr/bin/env python
#
# Copyright 2011 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS-IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

"""Module to handle searching for escrowed passphrases."""



import httplib
import logging
import os
import urllib
from google.appengine.api import users

from cauliflowervest.server import handlers
from cauliflowervest.server import models
from cauliflowervest.server import permissions
from cauliflowervest.server import util


MAX_VOLUMES_PER_QUERY = 999

SEARCH_TYPES = {
    permissions.TYPE_BITLOCKER: models.BitLockerVolume,
    permissions.TYPE_FILEVAULT: models.FileVaultVolume,
    permissions.TYPE_LUKS: models.LuksVolume,
    permissions.TYPE_PROVISIONING: models.ProvisioningVolume,
    }


def VolumesForQuery(q, search_type, prefix_search=False):
  """Search a model for matching the string query.

  Args:
    q: str search query.
    search_type: str key of SEARCH_TYPES constant.
    prefix_search: boolean, True to perform a prefix search, False otherwise.
  Returns:
    list of entities of type SEARCH_TYPES[search_type].
  Raises:
    ValueError: the given search_type is unknown.
  """
  if search_type not in SEARCH_TYPES:
    raise ValueError('Unknown search_type supplied: %r' % search_type)

  model = SEARCH_TYPES[search_type]
  query = model.all()

  fields = q.split(' ')
  for field in fields:
    try:
      name, value = field.strip().split(':')
    except ValueError:
      logging.info('Invalid field (%r) in query: %r', field, q)
      continue
    if name == 'created_by':
      if '@' not in value:
        value = '%s@%s' % (value, os.environ.get('AUTH_DOMAIN'))
      value = users.User(value)
    elif name == 'hostname':
      value = model.NormalizeHostname(value)

    if prefix_search and name != 'created_by':
      query.filter('%s >=' % name, value).filter(
          '%s <' % name, value + u'\ufffd')
    elif name == 'owner' and not prefix_search:
      # It turns out we store some owner names with the full email address
      # (e.g., exampleuser@google.com) and some without (e.g., exampleuser),
      # but when letting a user search their own, they may offer either one.
      if '@' in value and value.split('@')[1] == os.environ.get('AUTH_DOMAIN'):
        extra_owner_value = value.split('@')[0]
      else:
        extra_owner_value = '%s@%s' % (value, os.environ.get('AUTH_DOMAIN'))
      query.filter('owner IN', [value, extra_owner_value])
    else:
      query.filter(name + ' =', value)

  if (search_type == permissions.TYPE_PROVISIONING
      and len(fields) == 1
      and fields[0].strip().startswith('created_by:')):
    query.order('-created')
  volumes = query.fetch(MAX_VOLUMES_PER_QUERY)
  volumes.sort(key=lambda x: x.created, reverse=True)
  return volumes


class Search(handlers.AccessHandler):
  """Handler for /search URL."""

  def get(self):  # pylint: disable=g-bad-name
    """Handles GET requests."""
    # TODO(user): Users with retrieve_own should not need to search to
    # retrieve their escrowed secrets.
    if self.request.get('json', '0') != '1':
      search_type = self.request.get('search_type')
      field1 = urllib.quote(self.request.get('field1'))
      value1 = urllib.quote(self.request.get('value1').strip())
      prefix_search = urllib.quote(self.request.get('prefix_search', '0'))

      if search_type and field1 and value1:
        self.redirect(
            '/ui/#/search/%s/%s/%s/%s' % (
                search_type, field1, value1, prefix_search))
      else:
        self.redirect('/ui/')
      return

    tag = self.request.get('tag', 'default')
    search_type = self.request.get('search_type')
    field1 = self.request.get('field1')
    value1 = self.request.get('value1').strip()
    prefix_search = self.request.get('prefix_search', '0') == '1'

    if search_type not in SEARCH_TYPES:
      raise handlers.InvalidArgumentError(
          'Invalid search_type %s' % search_type)

    if not (field1 and value1):
      raise handlers.InvalidArgumentError('Missing field1 or value1')

    # Get the user's search and retrieve permissions for all permission types.
    search_perms = handlers.VerifyAllPermissionTypes(permissions.SEARCH)
    retrieve_perms = handlers.VerifyAllPermissionTypes(permissions.RETRIEVE_OWN)
    retrieve_created = handlers.VerifyAllPermissionTypes(
        permissions.RETRIEVE_CREATED_BY)

    # user is performing a search, ensure they have permissions.
    if (not search_perms.get(search_type)
        and not retrieve_perms.get(search_type)
        and not retrieve_created.get(search_type)):
      raise models.AccessDeniedError('User lacks %s permission' % search_type)

    # TODO(user): implement multi-field search by building query here
    #   or better yet using JavaScript.
    q = '%s:%s' % (field1, value1)
    try:
      volumes = VolumesForQuery(q, search_type, prefix_search)
    except ValueError:
      self.error(httplib.NOT_FOUND)
      return

    if not search_perms.get(search_type):
      username = models.GetCurrentUser().user.nickname()
      volumes = [x for x in volumes if x.owner == username]

    volumes = [v.ToDict(skip_secret=True) for v in volumes if v.tag == tag]
    if SEARCH_TYPES[search_type].ALLOW_OWNER_CHANGE:
      for volume in volumes:
        if not volume['active']:
          continue
        volume['change_owner_link'] = '/api/internal/change-owner/%s/%s/' % (
            search_type, volume['id'])

    self.response.out.write(util.ToSafeJson(volumes))
