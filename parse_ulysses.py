import os
from os.path import join
import subprocess
import biplist  # the built in plistlib does not support binary plist files.
import xml.etree.ElementTree as ET

from pyexpat import ExpatError

import workflow
import logging

# SHEET = "com.soulmen.ulysses3.sheet"
# GROUP = "com.soulmen.ulysses3.group"

ULYSSES3_ICLOUD_LIB = join(os.environ['HOME'], 'Library', 'Mobile Documents',
                    'X5AZV975AG~com~soulmen~ulysses3', 'Documents', 'Library')
ICLOUD_GROUPS_ROOT = join(ULYSSES3_ICLOUD_LIB, 'Groups-ulgroup')
ICLOUD_UNFILED_ROOT = join(ULYSSES3_ICLOUD_LIB, 'Unfiled-ulgroup')  # a.k.a. Inbox

ULYSSES3_LOCAL_LIB = join(os.environ['HOME'], 'Library', 'Containers',
                          'com.soulmen.ulysses3', 'Data', 'Documents',
                          'Library')
LOCAL_GROUPS_ROOT = join(ULYSSES3_LOCAL_LIB, 'Groups-ulgroup')
LOCAL_UNFILED_ROOT = join(ULYSSES3_LOCAL_LIB, 'Unfiled-ulgroup')  # a.k.a. Inbox


MDFIND_SHEET_QUERY = '((** = "%s*"cdw) && (kMDItemKind = "Ulysses Sheet*"cdwt))'
MDFIND_GROUP_QUERY = '((** = "%s*"cdw) && (kMDItemKind = "Ulysses Group*"cdwt))'


logger = workflow.Workflow3().logger
logger.setLevel(logging.DEBUG)

class Node:  # consider abstract
    is_group = 'override'
    is_sheet = 'override'

    def __init__(self, dirpath, parent_group):
        self.dirpath = dirpath
        self.parent_group = parent_group
        self.title = None

    def get_ancestors(self):
        ancestors = []
        ancestor = self.parent_group
        while ancestor:
            ancestors.append(ancestor)
            ancestor = ancestor.parent_group
        ancestors.reverse()
        return ancestors

    def get_alfred_path_list(self):
        return [a.name for a in self.get_ancestors()]


class Group(Node):

    is_group = True
    is_sheet = False

    def __init__(self, dirpath, parent_group):
        Node.__init__(self, dirpath, parent_group)
        self.child_groups = []
        self.child_sheets = []
        self.openable_file = join(self.dirpath, 'Info.ulgroup')
        self.name = self.title = self._get_group_name(dirpath)

    def _get_group_name(self, dirpath):
        if dirpath == ICLOUD_UNFILED_ROOT:
            return 'Inbox'  # No name in Info.ulgroup since Ulysses 12

        try:
            return biplist.readPlist(join(self.dirpath, 'Info.ulgroup'))['displayName']
        except KeyError:
            # Be defensive as filesystem is not a supported API and may change
            logger.warn("Key 'displayName' not found in: " + join(self.dirpath, 'Info.ulgroup'))
            return os.path.basename(os.path.normpath(dirpath)) # Use innermost directory
        except Exception as e:
            raise Exception("Error while reading '%s'" % join(self.dirpath, 'Info.ulgroup'), e)

    def number_descendents(self):
        n = len(self.child_sheets)
        for child_group in self.child_groups:
            n = n + child_group.number_descendents()
        return n


class Sheet(Node):

    is_group = False
    is_sheet = True

    def __init__(self, dirpath, parent_group):
        Node.__init__(self, dirpath, parent_group)
        self.dirpath = dirpath
        self.openable_file = dirpath

        _sheet_xml_path = join(self.dirpath, 'Content.xml')
        if os.path.exists(_sheet_xml_path):
            self.first_line = self._get_first_line_from_xml(_sheet_xml_path)
        else:
            _sheet_text_path = join(self.dirpath, 'Text.txt')
            if os.path.exists(_sheet_text_path):
                with open(_sheet_text_path, 'r') as f:
                    self.first_line = f.readline().decode('utf-8').strip()
            else:
                self.first_line = "Unknown Type"
        self.title = self.first_line

    def _get_first_line_from_xml(self, path):
        # Worth noting: this format is not an API. It first appeared in Ulysses 16. We'll want to be reasonably defensive here because it may change over time.

        # We incrementally parse the XML...
        for _, elem in ET.iterparse(path, parser=ET.XMLParser(encoding='utf-8')):
            # ...until we've parsed the first <p> tag
            if elem.tag == "p":
                # The paragraph may still have child tags. Here's an example:
                #   <p><tags><tag kind="heading1"># </tag></tags>Test note</p>
                # So we'll concatenate all text nodes in this paragraph to get the line.
                return "".join(elem.itertext()).strip()

        # This XML didn't contain any <p> tags. That might be OK: new sheets don't have any <p> tags.
        return ""


def filter_nodes_by_openable_file(nodes, openable_file_list):
    return [node for node in nodes if node.openable_file in openable_file_list]


def filter_groups(groups, query):
    openable_files = subprocess.check_output(
        ['mdfind', MDFIND_GROUP_QUERY % query]).split('\n')
    return filter_nodes_by_openable_file(groups, openable_files)


def filter_sheets(sheets, query):
    openable_files = subprocess.check_output(
        ['mdfind', MDFIND_SHEET_QUERY % query]).split('\n')
    return filter_nodes_by_openable_file(sheets, openable_files)


def create_tree(rootgroupdir, parent_group):
    '''recursively build group tree starting from rootgroupdir
    '''
    assert rootgroupdir.endswith('-ulgroup')
    filelist = os.listdir(rootgroupdir)
    assert 'Info.ulgroup' in filelist
    sheetdirlist = [p for p in filelist if p.endswith('.ulysses')]
    groupdirlist = [p for p in filelist if p.endswith('-ulgroup')]

    # Create group
    group = Group(rootgroupdir, parent_group)

    # Add Sheets
    for sheetdir in sheetdirlist:
        sheet = Sheet(join(rootgroupdir, sheetdir), group)
        group.child_sheets.append(sheet)

    # Recursively add groups
    for child_groupdir in groupdirlist:
        child_group = create_tree(join(rootgroupdir, child_groupdir), group)
        assert child_group != group
        group.child_groups.append(child_group)

    return group


def walk(root_group):
    """Walk a tree of nodes and return sheets and groups"""
    groups = [root_group]
    sheets = list(root_group.child_sheets)
    for child_group in root_group.child_groups:
        descendent_groups, descendent_sheets = walk(child_group)
        groups += descendent_groups
        sheets += descendent_sheets
    return groups, sheets


def find_group_by_path(root_group, dirpath):
    """Walk a tree of nodes and return the one backed by given dirpath

    KeyError if not found
    """
    groups, _ = walk(root_group)
    for group in groups:
        if group.dirpath == dirpath:
            return group
    raise KeyError("Group with dirpath '%s' not found" % dirpath)
