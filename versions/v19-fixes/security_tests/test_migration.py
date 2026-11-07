import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from agentic_memory.privacy.storage import MEMORY_SCHEMA
from agentic_memory.privacy.migrate import migrate
from agentic_memory.privacy import PrivacyMemorySystem
from agentic_memory.privacy.processors import HashEmbedder

class MigrationContract(unittest.TestCase):
    def test_legacy_is_read_only_and_new_pair_requires_reindex(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'legacy.sqlite';dest=Path(folder)/'new/memory.sqlite'
            db=sqlite3.connect(source);db.executescript(MEMORY_SCHEMA)
            db.executescript('CREATE TABLE policies(id TEXT PRIMARY KEY,revision INTEGER,body TEXT); CREATE TABLE memberships(group_id TEXT,principal_id TEXT); CREATE TABLE authority_owners(kind TEXT,id TEXT,issuer TEXT);')
            db.execute('INSERT INTO policies VALUES (?,?,?)',('p',1,'{"allows":[{"principals":["owner"],"actions":["read","derive"]}],"processors":["test:hash"]}'))
            db.execute("INSERT INTO authority_owners VALUES ('policy','p','owner')")
            db.execute("INSERT INTO resources VALUES ('r','memory','c','p','{\"text\":\"legacy\"}','{}','[]','[]','active',0)")
            db.commit();db.close()
            original=hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'Legacy single-store'):PrivacyMemorySystem(source,HashEmbedder())
            migrate(source,dest)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),original)
            core=PrivacyMemorySystem(dest,HashEmbedder())
            ctx=core.issue_session('owner','agent',actions=['read'],compartments=['c'])
            self.assertEqual(core.read(ctx,'r')['payload']['text'],'legacy')
            self.assertEqual(core.db.execute('SELECT count(*) FROM representations').fetchone()[0],0)
            self.assertEqual(core.db.execute('SELECT count(*) FROM access_edges').fetchone()[0],1)
            core.close()
            with self.assertRaises(ValueError):migrate(source,dest)
