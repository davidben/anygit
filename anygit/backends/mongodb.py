import datetime
import logging
import pymongo
import random
import re
import subprocess

from pymongo import son_manipulator
from pylons import config

from anygit.backends import common
from anygit.data import exceptions

logger = logging.getLogger(__name__)

max_transaction_window = 1000
curr_transaction_window = 0
connection = None
save_classes = []
collection_to_class = {}

## Exported functions

def create_schema():
    # Clear out the database
    GitObject._object_store.remove()
    GitObject._object_store.drop_index()
    GitObject._object_store.ensure_index({'type' : 1})
    GitObject._object_store.ensure_index({'repository_ids' : 1})

    Repository._object_store.remove()
    Repository._object_store.drop_index()
    Repository._object_store.ensure_index({'url' : 1})
    Repository._object_store.ensure_index({'been_indexed' : 1})
    Repository._object_store.ensure_index({'approved' : 1})
    Repository._object_store.ensure_index({'count' : 1})

def init_model(connection):
    """Call me before using any of the tables or classes in the model."""
    raw_db = connection.anygit
    db = connection.anygit
    # Transform
    db.add_son_manipulator(TransformObject())

    for obj in globals().itervalues():
        if type(obj) == type and issubclass(obj, MongoDbModel) and hasattr(obj, '__tablename__'):
            save_classes.append(obj)
            tablename = getattr(obj, '__tablename__')
            obj._object_store = getattr(db, tablename)
            obj._raw_object_store = getattr(raw_db, tablename)
            collection_to_class[obj._object_store] = obj

def setup():
    """
    Sets up the database session
    """
    global connection
    port = config.get('mongodb.port', None)
    if port:
        port = int(port)
    connection = pymongo.Connection(config['mongodb.url'],
                                    port)
    init_model(connection)

def flush():
    logger.debug('Committing...')
    for klass in save_classes:
        if klass._save_list:
            logger.debug('Saving %d %s instances...' % (len(klass._save_list), klass.__name__))

        for instance in klass._save_list:
            try:
                updates = instance.get_updates()
                klass._object_store.update({'_id' : instance.id},
                                           updates,
                                           upsert=True)
            except:
                logger.critical('Had some trouble saving %s' % instance)
                raise
            instance.mark_saved()
            instance.new = False
            instance._pending_save = False
            instance._changed = False
            instance._pending_updates.clear()
        klass._save_list = klass._save_list[0:0]
        klass._cache.clear()
        logger.debug('Saving %s complete.' % klass.__name__)

def destroy_session():
    if connection is not None:
        connection.disconnect()

## Internal functions

def classify(string):
    """Convert a class name to the corresponding class"""
    mapping = {'repository' : Repository,
               'blob' : Blob,
               'tree' : Tree,
               'commit' : Commit,
               'tag' : Tag}
    try:
        return mapping[string]
    except KeyError:
        raise ValueError('No matching class found for %s' % string)

def canonicalize_to_id(db_object):
    if isinstance(db_object, MongoDbModel):
        return db_object.id
    elif isinstance(db_object, basestring):
        return db_object
    else:
        raise exceptions.Error('Illegal type %s (instance %r)' % (type(db_object), db_object))

def canonicalize_to_object(id, cls=None):
    if not cls:
        cls = GitObject
    if isinstance(id, basestring):
        obj = cls.get(id=id)
    elif isinstance(id, cls):
        obj = id
        id = obj.id
    else:
        raise exceptions.Error('Illegal type %s (instance %r)' % (type(id), id))
    return id, obj

def sanitize_unicode(u):
    if isinstance(u, str):
        try:
            return unicode(u, 'utf-8')
        except UnicodeDecodeError:
            sanitized = unicode(u, 'iso-8859-1')
            logger.info('Invalid unicode detected: %r.  Assuming iso-8859-1 (%s)' % (u, sanitized))
            return sanitized
    else:
        return u

def convert_iterable(target, dest):
    if not hasattr(target, '__iter__'):
        return target
    elif not isinstance(target, dest):
        return dest(target)

def make_persistent_set():
    # TODO: transparently diff and persist this.
    backend_attr = '__%s' % hex(random.getrandbits(128))
    def _getter(self):
        if not hasattr(self, backend_attr):
            setattr(self, backend_attr, set())
        return getattr(self, backend_attr)
    def _setter(self, value):
        value = set(convert_iterable(entry, tuple) for entry in value)
        setattr(self, backend_attr, value)
    return property(_getter, _setter)

def make_persistent_attribute(name, default=None):
    backend_attr = '__%s' % hex(random.getrandbits(128))
    def _getter(self):
        if not hasattr(self, backend_attr):
            setattr(self, backend_attr, default)
        return getattr(self, backend_attr)
    def _setter(self, value):
        if hasattr(self, backend_attr) and value == getattr(self, backend_attr):
            return
        self._changed = True
        setting = self._pending_updates.setdefault('$set', {})
        setting[name] = value
        setattr(self, backend_attr, value)
    return property(_getter, _setter)

def rename_dict_keys(dict, to_backend=True):
    attrs = [('_id', 'id')]
    if to_backend:
        for backend, frontend in attrs:
            if frontend in dict:
                dict[backend] = dict[frontend]
                del dict[frontend]
    else:
        for backend, frontend in attrs:
            if backend in dict:
                dict[frontend] = dict[backend]
                del dict[backend]

## Classes


class Error(Exception):
    pass


class AbstractMethodError(Exception):
    pass


class TransformObject(son_manipulator.SONManipulator):
    def transform_incoming(self, object, collection):
        """Transform an object heading for the database"""
        return object

    def transform_outgoing(self, son, collection):
        """Transform an object retrieved from the database"""
        if 'type' in son:
            klass = classify(son['type'])
            return klass.demongofy(son)
        else:
            try:
                return collection_to_class[collection].demongofy(son)
            except KeyError:
                return son

class Map(object):
    def __init__(self, result, fun):
        self.result = result
        self._iterator = (fun(i) for i in result)

    def __iter__(self):
        return iter(self._iterator)

    def count(self):
        return self.result.count()

    def next(self):
        return self._iterator.next()
        

class MongoDbModel(object):
    # Should provide these in subclasses
    _cache = {}
    _save_list = None
    batched = True
    has_type = True

    # Attributes: id, type

    def __init__(self, _raw_dict={}, **kwargs):
        rename_dict_keys(kwargs, to_backend=True)
        self._pending_updates = {}
        self._init_from_dict(_raw_dict)
        self._pending_updates.clear()
        self._init_from_dict(kwargs)
        self.new = True
        self._pending_save = False
        self._changed = False

    def _init_from_dict(self, dict):
        rename_dict_keys(dict, to_backend=False)
        for k, v in dict.iteritems():
            if k == 'type':
                assert v == self.type
                continue
            setattr(self, k, v)

    def _add_all_to_set(self, set_name, values):
        # TODO: to get the *right* semantics, should have a committed updates
        # and an uncommitted updates.
        assert isinstance(values, set)
        full_set = getattr(self, set_name)
        # Get rid of everything we already have
        values = values.difference(full_set)
        if not values:
            return
        full_set.update(values)
        adding = self._pending_updates.setdefault('$addToSet', {})
        target_set = adding.setdefault(set_name, {'$each' : []})
        target_set['$each'].extend(values)
        
    def _add_to_set(self, set_name, value):
        return self._add_all_to_set(set_name, set([value]))

    @property
    def type(self):
        return type(self).__name__.lower()

    @classmethod
    def find(cls, kwargs):
        return cls._object_store.find(kwargs)

    @classmethod
    def get(cls, id):
        """Get an item with the given primary key"""
        cached = cls.get_from_cache(id=id)
        if cached:
            return cached
        else:
            return cls.get_by_attributes(id=id)

    @classmethod
    def get_from_cache_or_new(cls, id):
        cached = cls.get_from_cache(id=id)
        if cached:
            return cached
        else:
            return cls(id=id)

    @classmethod
    def get_from_cache(cls, id):
        if cls._cache and id in cls._cache:
            return cls._cache[id]
        else:
            return None

    @classmethod
    def get_by_attributes(cls, **kwargs):
        rename_dict_keys(kwargs, to_backend=True)
        results = cls.find(kwargs)
        count = results.count()
        if count == 1:
            result = results.next()
            if cls != GitObject:
                try:
                    assert isinstance(result, cls)
                except AssertionError:
                    logger.critical('Corrupt data %s, should be a %s' % (result, cls.__name__))
                    raise
            return result
        elif count == 0:
            raise exceptions.DoesNotExist('%s: %s' % (cls.__name__, kwargs))
        else:
            raise exceptions.NotUnique('%s: %s' % (cls.__name__, kwargs))

    @classmethod
    def all(cls):
        return cls._object_store.find()

    @classmethod
    def exists(cls, **kwargs):
        rename_dict_keys(kwargs, to_backend=True)
        return cls._object_store.find(kwargs).count() > 0

    def refresh(self):
        dict = self._raw_object_store.find_one({'_id' : self.id})
        self._init_from_dict(dict)

    def validate(self):
        """A stub method.  Should be overriden in subclasses."""
        pass

    @property
    def changed(self):
        """Indicate whether this object is changed from the version in
        the database.  Returns True for new objects."""
        return self.new or self._changed or self._pending_updates

    def save(self):
        global curr_transaction_window
        self.validate()
        if not self._errors:
            if not (self.changed or self.new):
                return True
            elif self.batched:
                self._cache[self.id] = self
                self._save_list.append(self)
                if self._pending_save:
                    return
                self._pending_save = True
                if curr_transaction_window >= max_transaction_window:
                    flush()
                    curr_transaction_window = 0
                else:
                    curr_transaction_window += 1
            else:
                raise NotImplementedError('Non batched saves are not supported')
            return True
        else:
            return False

    def delete(self):
        raise NotImplementedError()

    def mongofy(self, mongo_object=None):
        if mongo_object is None:
            return {}
        mongo_object['_id'] = self.id
        return mongo_object

    @classmethod
    def demongofy(cls, son):
        if '_id' in son:
            son['id'] = son['_id']
            del son['_id']
        elif 'id' not in son:
            raise exceptions.ValidationError('Missing attribute id in %s' % son)
        instance = cls(_raw_dict=son)
        instance.new = False
        return instance

    @classmethod
    def find_matching(cls, ids, **kwargs):
        """Given a list of ids, find the matching objects"""
        kwargs.update({'_id' : { '$in' : list(ids) }})
        return cls._object_store.find(kwargs)

    @classmethod
    def count_instances(cls, **kwargs):
        """Find the number of objects that match the given criteria"""
        kwargs['type'] = cls.__name__.lower()
        return cls._object_store.find(kwargs).count()

    def get_updates(self):
        # Hack to add *something* for new insertions
        if self.has_type:
            self._pending_updates.setdefault('$set', {}).setdefault('type', self.type)
        elif not self._pending_updates:
            # Doing an upsert requires a non-empty object, so put in something small
            self._pending_updates.setdefault('$set', {}).setdefault('d', 1)
        return self._pending_updates

    def mark_saved(self):
        self.new = False
        self._pending_save = False
        self._changed = False
        self._pending_updates.clear()

    def __str__(self):
        return '%s: %s' % (self.type, self.id)

    def __repr__(self):
        return str(self)

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return self.id == other.id


class GitObjectAssociation(MongoDbModel, common.CommonMixin):
    has_type = False
    key1_name = None
    key2_name = None

    def __init__(self, key1=None, key2=None, _raw_dict={}):
        super(GitObjectAssociation, self).__init__(_raw_dict=_raw_dict)
        if key1:
            setattr(self, self.key1_name, key1)
        if key2:
            setattr(self, self.key2_name, key2)
        if key1 and key2:
            self._id = key1 + key2

    def mongofy(self, mongo_object=None):
        if mongo_object is None:
            return {}
        super(GitObjectAssociation, self).mongofy(mongo_object)
        mongo_object[self.key1_name] = getattr(self, self.key1_name)
        mongo_object[self.key2_name] = getattr(self, self.key2_name)
        return mongo_object

    @property
    def id(self):
        return self._id

    @id.setter
    def id(self, value):
        assert len(value) == 80
        setattr(self, self.key1_name, value[0:40])
        setattr(self, self.key2_name, value[40:80])

    @classmethod
    def get_all(cls, sha1):
        safe_sha1 = '^%s' % re.escape(sha1)
        return cls._object_store.find({'_id' : re.compile(safe_sha1)})

    def __str__(self):
        return '%s: %s=%s, %s=%s' % (self.type,
                                     self.key1_name, getattr(self, self.key1_name),
                                     self.key2_name, getattr(self, self.key2_name))


class BlobTree(GitObjectAssociation):
    __tablename__ = 'blob_trees'
    _save_list = []
    _cache = {}
    key1_name = 'blob_id'
    key2_name = 'tree_id'

    name = make_persistent_attribute('name')

    def mongofy(self, mongo_object={}):
        if mongo_object is None:
            return {}
        super(BlobTree, self).mongofy(mongo_object)
        mongo_object['name'] = self.name
        return mongo_object


class BlobTag(GitObjectAssociation):
    __tablename__ = 'blob_tags'
    _save_list = []
    _cache = {}
    key1_name = 'blob_id'
    key2_name = 'tag_id'


class TreeParentTree(GitObjectAssociation):
    __tablename__ = 'tree_parent_trees'
    _save_list = []
    _cache = {}
    key1_name = 'tree_id'
    key2_name = 'parent_tree_id'

    name = make_persistent_attribute('name')

    def mongofy(self, mongo_object=None):
        if mongo_object is None:
            return {}
        super(TreeParentTree, self).mongofy(mongo_object)
        mongo_object['name'] = self.name
        return mongo_object

class TreeCommit(GitObjectAssociation):
    __tablename__ = 'tree_commits'
    _save_list = []
    _cache = {}
    key1_name = 'tree_id'
    key2_name = 'commit_id'


class TreeTag(GitObjectAssociation):
    __tablename__ = 'tree_tags'
    _save_list = []
    _cache = {}
    key1_name = 'tree_id'
    key2_name = 'tag_id'


class CommitParentCommit(GitObjectAssociation):
    __tablename__ = 'commit_parent_commits'
    _save_list = []
    _cache = {}
    key1_name = 'commit_id'
    key2_name = 'parent_commit_id'


class CommitTree(GitObjectAssociation):
    __tablename__ = 'commit_trees'
    _save_list = []
    _cache = {}
    key1_name = 'commit_id'
    key2_name = 'tree_id'

    name = make_persistent_attribute('name')

    def mongofy(self, mongo_object=None):
        if mongo_object is None:
            return {}
        super(CommitTree, self).mongofy(mongo_object)
        mongo_object['name'] = self.name
        return mongo_object


class CommitTag(GitObjectAssociation):
    __tablename__ = 'commit_tags'
    _save_list = []
    _cache = {}
    key1_name = 'commit_id'
    key2_name = 'tag_id'


class TagParentTag(GitObjectAssociation):
    __tablename__ = 'tag_parent_tags'
    _save_list = []
    _cache = {}
    key1_name = 'tag_id'
    key2_name = 'parent_tag_id'


class GitObject(MongoDbModel, common.CommonGitObjectMixin):
    """The base class for git objects (such as blobs, commits, etc..)."""
    # Attributes: repository_ids, tag_ids, dirty
    __tablename__ = 'git_objects'
    _save_list = []
    _cache = {}
    repository_ids = make_persistent_set()
    dirty = make_persistent_attribute('dirty')

    @classmethod
    def lookup_by_sha1(cls, sha1, partial=False, offset=0, limit=10):
        # TODO: might want to disable lookup for dirty objects, or something
        if partial:
            safe_sha1 = '^%s' % re.escape(sha1)
            results = cls._object_store.find({'_id' : re.compile(safe_sha1)})
        else:
            results = cls._object_store.find({'_id' : sha1})
        count = results.count()
        return results.skip(offset).limit(limit), count

    @classmethod
    def all(cls):
        if cls == GitObject:
            return cls._object_store.find()
        else:
            return cls._object_store.find({'type' : cls.__name__.lower()})

    def mark_dirty(self, value):
        self.dirty = value

    @property
    def repositories(self):
        return Repository.find_matching(self.repository_ids)

    def add_tag(self, tag_id):
        raise AbstractMethodError()

    @property
    def tags(self):
        return Tag.find_matching(self.tag_ids)


class Blob(GitObject, common.CommonBlobMixin):
    """Represents a git Blob.  Has an id (the sha1 that identifies this
    object)"""

    def add_parent(self, parent_id, name):
        name = sanitize_unicode(name)
        parent_id = canonicalize_to_id(parent_id)
        b = BlobTree(key1=self.id, key2=parent_id)
        b.name = name
        b.save()

    @property
    def parent_ids_with_names(self):
        return Map(BlobTree.get_all(self.id), lambda bt: (bt.tree_id, bt.name))

    @property
    def parent_ids(self):
        return (id for (id, name) in self.parent_ids_with_names)

    @property
    def names(self):
        return set(name for (id, name) in self.parent_ids_with_names)

    @property
    def parents(self):
        return Tree.find_matching(self.parent_ids)

    @property
    def parents_with_names(self):
        return set((Tree.get(id), name) for (id, name) in self.parent_ids_with_names)

    def add_repository(self, repository_id, recursive=False):
        repository_id = canonicalize_to_id(repository_id)
        self._add_to_set('repository_ids', repository_id)


### Still working here.

class Tree(GitObject, common.CommonTreeMixin):
    """Represents a git Tree.  Has an id (the sha1 that identifies this
    object)"""

    def add_parent(self, parent_id, name):
        """Give this tree a parent.  Also updates the parent to know
        about this tree."""
        name = sanitize_unicode(name)
        parent_id = canonicalize_to_id(parent_id)
        b = TreeParentTree(key1=self.id, key2=parent_id)
        b.name = name
        b.save()

    @property
    def parent_ids_with_names(self):
        return Map(TreeParentTree.get_all(self.id), lambda tpt: (tpt.tree_id, tpt.name))

    def add_commit(self, commit_id):
        commit_id = canonicalize_to_id(commit_id)
        t = TreeCommit(key1=self.id, key2=commit_id)
        t.save()

    @property
    def commit_ids(self):
        return Map(TreeCommit.get_all(self.id), lambda tc: tc.commit_id)

    @property
    def commits(self):
        return Commit.find_matching(self.commit_ids)

    @property
    def parent_ids(self):
        return set(id for (id, name) in self.parent_ids_with_names)

    @property
    def names(self):
        return set(name for (id, name) in self.parent_ids_with_names)

    @property
    def parents(self):
        return Tree.find_matching(self.parent_ids)

    @property
    def parents_with_names(self):
        return set((Tree.get(id), name) for (id, name) in self.parent_ids_with_names)

    def add_repository(self, repository_id):
        repository_id = canonicalize_to_id(repository_id)
        self._add_to_set('repository_ids', repository_id)
        self.save()


class Tag(GitObject, common.CommonTagMixin):
    """Represents a git Tree.  Has an id (the sha1 that identifies this
    object)"""

    def add_repository(self, repository_id):
        repository_id = canonicalize_to_id(repository_id)
        self._add_to_set('repository_ids', repository_id)
        self.save()


class Commit(GitObject, common.CommonCommitMixin):
    """Represents a git Commit.  Has an id (the sha1 that identifies
    this object).  Also contains blobs, trees, and tags."""
    parent_ids = make_persistent_set()

    def add_repository(self, repository_id, recursive=False):
        repository_id = canonicalize_to_id(repository_id)
        self._add_to_set('repository_ids', repository_id)
        self.save()

    def add_parent(self, parent):
        self.add_parents([parent])

    def add_parents(self, parent_ids):
        parent_ids = set(canonicalize_to_id(p) for p in parent_ids)
        self._add_all_to_set('parent_ids', parent_ids)

    def add_as_submodule_of(self, tree_id, name):
        tree_id = canonicalize_to_id(tree_id)
        name = sanitize_unicode(name)
        b = CommitTree(key1=self.id, key2=tree_id)
        b.name = name
        b.save()

    @property
    def submodule_of_with_names(self):
        return Map(CommitTree.get_all(self.id), lambda ct: (ct.tree_id, ct.name))

    @property
    def submodule_of(self):
        return set(id for (id, name) in self.submodule_of_with_names)

    @property
    def parents(self):
        return Commit.find_matching(self.parent_ids)


class Repository(MongoDbModel, common.CommonRepositoryMixin):
    """A git repository.  Contains many commits."""
    _save_list = []
    __tablename__ = 'repositories'

    # Attributes: url, last_index, indexing, commit_ids
    url = make_persistent_attribute('url')
    last_index = make_persistent_attribute('last_index', default=datetime.datetime(1970,1,1))
    indexing = make_persistent_attribute('indexing', default=False)
    commit_ids = make_persistent_set()
    been_indexed = make_persistent_attribute('been_indexed', default=False)
    approved = make_persistent_attribute('approved', default=False)
    count = make_persistent_attribute('count', default=0)

    def __init__(self, *args, **kwargs):
        super(Repository, self).__init__(*args, **kwargs)
        # TODO: persist this.
        if not hasattr(self, 'remote_heads'):
            self.remote_heads = {}

    @classmethod
    def get_indexed_before(cls, date):
        """Get all repos indexed before the given date and not currently
        being indexed."""
        if date is not None:
            return cls._object_store.find({'last_index' : {'$lt' : date},
                                           'indexing' : False,
                                           'approved' : True})
        else:
            return cls._object_store.find({'indexing' : False,
                                           'approved' : True})

    @classmethod
    def get_by_highest_count(cls, n=None, descending=True):
        if descending:
            order = pymongo.DESCENDING
        else:
            order = pymongo.ASCENDING
        base = cls._object_store.find().sort('count', order)
        if n:
            full = base.limit(n)
        else:
            full = base
        return full

    def set_count(self, value):
        self.count = value

    def count_objects(self):
        return GitObject._object_store.find({'repository_ids' : self.id}).count()

    def __str__(self):
        return 'Repository: %s' % self.url
