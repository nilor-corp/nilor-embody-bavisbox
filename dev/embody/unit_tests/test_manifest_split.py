"""
Test suite: externalizations manifest split + deterministic ordering (P0).

Covers the merge-cleanliness work:
  - _sortTableByPath / _sortManifest produce a canonical ascending order and
    are a no-op when already sorted.
  - _migrateTableSchema splits the legacy combined table (volatile columns
    inline) into the 4-column tracked manifest + the git-ignored sidecar.
  - Volatile reads degrade gracefully when the sidecar is missing (fresh
    clone where externalizations.local.tsv was git-ignored and absent).

These tests isolate themselves from the live tables by swapping
par.Externalizations to a synthetic table and monkeypatching the sidecar
accessors to point at a sandbox table, restoring everything in tearDown.
"""

runner_mod = op.unit_tests.op('TestRunnerExt').module
EmbodyTestCase = runner_mod.EmbodyTestCase


class TestManifestSplit(EmbodyTestCase):

    def setUp(self):
        super().setUp()
        self._original_table = self.embody.par.Externalizations.eval()
        self._ext_class = type(self.embody_ext)
        self._patched = {}

    def tearDown(self):
        # Restore class-level monkeypatches first.
        for name, val in self._patched.items():
            if val is None:
                # attribute was not originally present
                try:
                    delattr(self._ext_class, name)
                except AttributeError:
                    pass
            else:
                setattr(self._ext_class, name, val)
        # Restore the live Externalizations parameter.
        if self._original_table is not None:
            self.embody.par.Externalizations = self._original_table.path
        super().tearDown()

    def _patch(self, name, value):
        self._patched[name] = self._ext_class.__dict__.get(name)
        setattr(self._ext_class, name, value)

    def _redirect_sidecar(self, side):
        """Point MetaTable / _ensureSidecar at a sandbox sidecar table."""
        self._patch('MetaTable', property(lambda self_: side))
        self._patch('_ensureSidecar', lambda self_: side)

    # =====================================================================
    # _sortTableByPath / _sortManifest
    # =====================================================================

    def _make_manifest(self, name, paths):
        tbl = self.sandbox.create(tableDAT, name)
        tbl.clear()
        tbl.appendRow(list(self.embody_ext.MANIFEST_COLS))
        for p in paths:
            tbl.appendRow([p, 'base', 'tox', p.lstrip('/') + '.tox'])
        return tbl

    def test_sort_orders_rows_ascending(self):
        tbl = self._make_manifest(
            'sort_unsorted', ['/c/z', '/a/b', '/a/a', '/b'])
        self.embody_ext._sortTableByPath(tbl)
        ordered = [tbl[i, 0].val for i in range(1, tbl.numRows)]
        self.assertListEqual(ordered, ['/a/a', '/a/b', '/b', '/c/z'])
        # Header preserved as the first row.
        self.assertEqual(tbl[0, 0].val, 'path')

    def test_sort_is_idempotent(self):
        tbl = self._make_manifest('sort_sorted', ['/a', '/b', '/c'])
        before = [tbl[i, 0].val for i in range(tbl.numRows)]
        self.embody_ext._sortTableByPath(tbl)
        after = [tbl[i, 0].val for i in range(tbl.numRows)]
        self.assertListEqual(before, after)

    def test_sort_noop_on_tiny_table(self):
        tbl = self.sandbox.create(tableDAT, 'sort_tiny')
        tbl.clear()
        tbl.appendRow(list(self.embody_ext.MANIFEST_COLS))
        # No data rows -- must not raise.
        self.embody_ext._sortTableByPath(tbl)
        self.assertEqual(tbl.numRows, 1)

    # =====================================================================
    # _migrateTableSchema: combined -> split
    # =====================================================================

    def test_migration_splits_combined_table(self):
        combined = self.sandbox.create(tableDAT, 'combined')
        combined.clear()
        combined.appendRow([
            'path', 'type', 'strategy', 'rel_file_path', 'timestamp',
            'dirty', 'build', 'touch_build', 'node_x', 'node_y', 'node_color'
        ])
        combined.appendRow([
            '/sb/alpha', 'base', 'tox', 'sb/alpha.tox',
            '2026-01-01 00:00:00 UTC', '', '7', '099.x', '100', '200',
            '0.1,0.2,0.3'
        ])
        combined.appendRow([
            '/sb/beta', 'text', 'py', 'sb/beta.py',
            '2026-02-02 00:00:00 UTC', 'Par', '', '', '0', '0', ''
        ])

        side = self.sandbox.create(tableDAT, 'combined_side')
        side.clear()
        side.appendRow(list(self.embody_ext.SIDECAR_HEADER))

        self.embody.par.Externalizations = combined.path
        self._redirect_sidecar(side)

        self.embody_ext._migrateTableSchema()

        # Tracked table now holds only the 4 manifest columns.
        headers = [combined[0, c].val for c in range(combined.numCols)]
        self.assertListEqual(headers, list(self.embody_ext.MANIFEST_COLS))

        # Volatile data moved into the sidecar, keyed by path.
        self.assertEqual(self.embody_ext._metaGet('/sb/alpha', 'build'), '7')
        self.assertEqual(
            self.embody_ext._metaGet('/sb/alpha', 'node_color'), '0.1,0.2,0.3')
        self.assertEqual(self.embody_ext._metaGet('/sb/beta', 'dirty'), 'Par')
        self.assertEqual(
            self.embody_ext._metaGet('/sb/beta', 'timestamp'),
            '2026-02-02 00:00:00 UTC')

    def test_migration_is_idempotent(self):
        already_split = self.sandbox.create(tableDAT, 'split_already')
        already_split.clear()
        already_split.appendRow(list(self.embody_ext.MANIFEST_COLS))
        already_split.appendRow(['/sb/x', 'base', 'tox', 'sb/x.tox'])

        side = self.sandbox.create(tableDAT, 'split_already_side')
        side.clear()
        side.appendRow(list(self.embody_ext.SIDECAR_HEADER))

        self.embody.par.Externalizations = already_split.path
        self._redirect_sidecar(side)

        # Running twice must not change the already-split tracked schema.
        self.embody_ext._migrateTableSchema()
        self.embody_ext._migrateTableSchema()
        headers = [already_split[0, c].val
                   for c in range(already_split.numCols)]
        self.assertListEqual(headers, list(self.embody_ext.MANIFEST_COLS))

    # =====================================================================
    # Graceful degradation when the sidecar is missing (fresh clone)
    # =====================================================================

    def test_metaGet_returns_default_without_sidecar(self):
        self._patch('MetaTable', property(lambda self_: None))
        self.assertEqual(self.embody_ext._metaGet('/no/op', 'build'), '')
        self.assertEqual(
            self.embody_ext._metaGet('/no/op', 'build', default='1'), '1')

    def test_restorePosition_noop_without_sidecar(self):
        self._patch('MetaTable', property(lambda self_: None))
        comp = self.sandbox.create(baseCOMP, 'pos_comp')
        comp.nodeX = 42
        comp.nodeY = 24
        # Must not raise and must leave position untouched.
        self.embody_ext._restorePositionFromTable(comp, comp.path)
        self.assertEqual(int(comp.nodeX), 42)
        self.assertEqual(int(comp.nodeY), 24)

    def test_reconstructAboutPage_noop_without_metadata(self):
        self._patch('MetaTable', property(lambda self_: None))
        comp = self.sandbox.create(baseCOMP, 'about_comp')
        # No sidecar metadata -> no About page is fabricated, no crash.
        self.embody_ext._reconstructAboutPage(comp, comp.path)
        about = next((p for p in comp.customPages if p.name == 'About'), None)
        self.assertIsNone(about)
