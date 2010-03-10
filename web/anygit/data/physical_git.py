import git.blob
import git.commit
import git.repo
import git.tag
import git.tree
import hashlib
import os
import re
import subprocess

THE_ONE_REPO_PATH = '/home/greg/repositories/anygit/the-one-repo'
split_re = re.compile(r'[ \t]')
hexdigest_re = re.compile(r'^[0-9a-fA-F]{40}$')

def classify(type):
    mapping = {'blob' : git.blob.Blob,
               'commit' : git.commit.Commit,
               'tag' : git.tag.Tag,
               'tree' : git.tree.Tree}
    return mapping[type]

def sha1(string):
    return hashlib.sha1(string).hexdigest()

class Error(Exception):
    pass

class GitCallError(Error):
    pass

class PhysicalRepo(object):
    def __init__(self, path):
        self.path = os.path.abspath(path)

    def run(self, *args, **kwargs):
        split = kwargs.get('split', False)

        cmd = ['git'] + list(args)
        process = subprocess.Popen(cmd,
                                   stdout=subprocess.PIPE,
                                   cwd=self.path)
        print cmd, 'in', self.path
        result = process.communicate()[0]
        if process.returncode:
            raise GitCallError('Git command %s returned %d' % (cmd, process.returncode))
        if split:
            if result.endswith('\n'):
                result = result[:-1]
            return result.split('\n')
        else:
            return result

    def add_remote(self, url, localname=None):
        if localname is None:
            localname = sha1(url)
        self.run('remote', 'add', localname, url)
        return localname

    def normalize_name(self, name):
        if hexdigest_re.search(name):
            return name
        else:
            return sha1(name)

    def fetch(self, remote):
        remote = self.normalize_name(remote)
        return self.run('fetch', remote)

    def list_branches(self, remote):
        remote = self.normalize_name(remote)
        raw_result = self.run('for-each-ref', 'refs/remotes/%s/*' % remote,
                              split=True)
        # A result line looks like
        # sha1 commit\trefs/remotes/REMOTE/BRANCH
        branches = [split_re.split(line)[2].split('/')[-1]
                    for line in raw_result]
        return branches

    def list_commits(self, remote, branch):
        remote = self.normalize_name(remote)
        raw_result = self.run('rev-list', 'refs/remotes/%s/%s' %
                              (remote, branch), split=True)
        return raw_result

    def list_blobs(self, commit):
        raw_result = self.run('ls-tree', '-rt', commit, split=True)
        # sample line
        # 100644 blob 60f00ef0ef347811e7...\tweb/anygit/root/views.py
        result = [split_re.split(line)[2] for line in raw_result]
        return result

THE_ONE_REPO = PhysicalRepo(path=THE_ONE_REPO_PATH)
