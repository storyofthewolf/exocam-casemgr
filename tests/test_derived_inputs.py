#!/usr/bin/env python3
"""
Tests for derived inputs (build.py prep / derive_utils.py), 2026-09-13.

Covers the pure layer (naming, star parsing, planning, the effective
exo_solar_file/ncdata, warnings vs errors), the emitted shell (required-inputs
guard, albedo upsert executed against a real namelist), the registry
round-trip (scan reads the manifests back; export emits physics keys, not
product paths), and -- when the local ExoCAM/ExoRT checkouts are present --
the engines themselves: the pressure rescale must reproduce the IDL-made
ic_*bar files byte for byte, and the albedo calculator must return the
reference solar numbers.

Run:  python3 tests/test_derived_inputs.py
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import build      # noqa: E402
import derive_utils   # noqa: E402
import query      # noqa: E402
import scan       # noqa: E402

EXOCAM = '/Users/wolfe/models/ExoCAM'
EXORT = '/Users/wolfe/models/ExoRT'
IC_DIR = f'{EXOCAM}/cesm1.2.1/initial_files/cam_aqua_fv'
HAVE_ENGINES = (os.path.isfile(f'{IC_DIR}/control_L40.cam.i.0009-01-01-00000.nc')
                and os.path.isfile(f'{EXORT}/data/solar/G2V_SUN_n68.nc'))

REGISTRY = {
    'paths': {'exocam_root': '/hpc/ExoCAM', 'exort_root': '/hpc/ExoRT',
              'derived_inputs': '/hpc/derived'},
    'ic_templates': {'ctrl40': 'cam_aqua_fv/control_L40.cam.i.0009-01-01-00000.nc',
                     'abs': '/somewhere/else.nc'},
    'ic_files': {},
}


def base_spec(**over):
    spec = {'config_type': 'cam_aqua_fv', 'exort_pkg': 'n68equiv', 'nlev': 40,
            'exo_co2bar': 0.01, 'exo_n2bar_explicit': 1.99}
    spec.update(over)
    return spec


def plan(spec, registry=REGISTRY):
    paths = registry['paths']
    pstd = build.compute_pstd_from_spec(spec)
    return derive_utils.plan_derived_inputs(spec, registry, paths, pstd_bar=pstd,
                                      pressure_str=build.bar_to_pressure_str(pstd))


class Naming(unittest.TestCase):
    def test_fmt_num(self):
        self.assertEqual(derive_utils.fmt_num(3400.0), '3400')
        self.assertEqual(derive_utils.fmt_num(4.5), '4.5')
        self.assertEqual(derive_utils.fmt_num(0.0), '0.0')
        self.assertEqual(derive_utils.fmt_num(-0.5), '-0.5')
        self.assertEqual(derive_utils.fmt_num(5.0), '5.0')

    def test_solar_and_sed_names(self):
        star = derive_utils.parse_star({'teff': 3400, 'feh': 0, 'logg': 4.5})
        self.assertEqual(derive_utils.solar_name(star, 'n68'), 'btsettl_T3400_g4.5_m0.0_n68.nc')
        self.assertEqual(derive_utils.sed_name(star), 'btsettl_T3400_g4.5_m0.0.txt')

    def test_ic_name(self):
        self.assertEqual(derive_utils.ic_name('ctrl40', '2bar', 40, True), 'ic_ctrl40_2bar_L40_dry_ic.nc')
        self.assertEqual(derive_utils.ic_name('ctrl51', '0.1bar', 51, False), 'ic_ctrl51_0.1bar_L51_ic.nc')

    def test_albedo_product_name(self):
        self.assertEqual(derive_utils.albedo_product_name('/x/G2V_SUN_n68.nc', 'mixed'),
                         'G2V_SUN_n68__mixed.albedos.sh')

    def test_parse_star(self):
        self.assertIsNone(derive_utils.parse_star(None))
        self.assertEqual(derive_utils.parse_star('SUN'), 'sun')
        s = derive_utils.parse_star({'teff': 3400})
        self.assertEqual((s['teff'], s['feh'], s['logg'], s['method']), (3400.0, 0.0, 4.5, 'log'))
        with self.assertRaises(derive_utils.DeriveError):
            derive_utils.parse_star('vega')
        with self.assertRaises(derive_utils.DeriveError):
            derive_utils.parse_star({'feh': 0.0})
        with self.assertRaises(derive_utils.DeriveError):
            derive_utils.parse_star({'teff': 3400, 'bogus': 1})
        with self.assertRaises(derive_utils.DeriveError):
            derive_utils.parse_star({'teff': 3400, 'method': 'cubic'})


class Planning(unittest.TestCase):
    def test_silent_matrix_derives_nothing(self):
        items, eff, warns = plan(base_spec())
        self.assertEqual(items, [])
        self.assertEqual(eff, {})
        self.assertEqual(warns, [])

    def test_star_dict_full_chain_a_plus_ic(self):
        items, eff, warns = plan(base_spec(star={'teff': 3400}, ic_template='ctrl40'))
        kinds = [it.kind for it in items]
        self.assertEqual(kinds, ['solar', 'albedo', 'ic'])
        self.assertEqual(eff['exo_solar_file'], '/hpc/derived/solar/btsettl_T3400_g4.5_m0.0_n68.nc')
        self.assertEqual(eff['albedo_product'],
                         '/hpc/derived/albedo/btsettl_T3400_g4.5_m0.0_n68__blueice.albedos.sh')
        self.assertEqual(eff['ncdata'], '/hpc/derived/ic/cam_aqua_fv/ic_ctrl40_2bar_L40_dry_ic.nc')
        ic = items[2].recipe
        self.assertEqual(ic['template'], '/hpc/ExoCAM/cesm1.2.1/initial_files/cam_aqua_fv/control_L40.cam.i.0009-01-01-00000.nc')
        self.assertEqual((ic['pstd_bar'], ic['nlev'], ic['dry']), (2.0, 40, True))
        self.assertEqual(warns, [])

    def test_star_sun_uses_shipped_solar_and_derives_albedos_only(self):
        items, eff, _ = plan(base_spec(star='sun', ice_surface='mixed'))
        self.assertEqual([it.kind for it in items], ['albedo'])
        self.assertEqual(eff['exo_solar_file'], '/hpc/ExoRT/data/solar/G2V_SUN_n68.nc')
        self.assertTrue(eff['albedo_product'].endswith('G2V_SUN_n68__mixed.albedos.sh'))

    def test_resolution_follows_exort_pkg(self):
        items, eff, _ = plan(base_spec(star={'teff': 3000}, exort_pkg='n28archean'))
        self.assertTrue(eff['exo_solar_file'].endswith('_n28.nc'))
        _, eff2, _ = plan(base_spec(star='sun', exort_pkg='n84equiv'))
        self.assertTrue(eff2['exo_solar_file'].endswith('G2V_SUN_n84.nc'))

    def test_explicit_values_win_with_warnings(self):
        spec = base_spec(star={'teff': 3400}, ic_template='ctrl40',
                         exo_solar_file='/my/star.nc', ncdata='/my/ic.nc',
                         cice_params={'albicev': 0.5})
        items, eff, warns = plan(spec)
        self.assertEqual(items, [])
        self.assertEqual(eff, {})
        self.assertEqual(len(warns), 3)
        self.assertTrue(any('explicit file wins' in w for w in warns))
        self.assertTrue(any('explicit albedos win' in w for w in warns))
        self.assertTrue(any('explicit ncdata wins' in w for w in warns))

    def test_derive_albedos_false(self):
        items, eff, _ = plan(base_spec(star='sun', derive_albedos=False))
        self.assertEqual(items, [])
        self.assertNotIn('albedo_product', eff)
        self.assertIn('exo_solar_file', eff)

    def test_derive_albedos_without_star_warns(self):
        items, _, warns = plan(base_spec(derive_albedos=True))
        self.assertEqual(items, [])
        self.assertTrue(any("no 'star' key" in w for w in warns))

    def test_unknown_template_is_an_error(self):
        with self.assertRaises(derive_utils.DeriveError) as cm:
            plan(base_spec(ic_template='nope'))
        self.assertIn('nope', str(cm.exception))
        self.assertIn('ctrl40', str(cm.exception))

    def test_absolute_template_used_verbatim(self):
        items, _, _ = plan(base_spec(ic_template='abs'))
        self.assertEqual(items[0].recipe['template'], '/somewhere/else.nc')

    def test_bad_ice_surface_is_an_error(self):
        with self.assertRaises(derive_utils.DeriveError):
            plan(base_spec(star='sun', ice_surface='asphalt'))

    def test_unknown_exort_pkg_cannot_derive_solar(self):
        with self.assertRaises(derive_utils.DeriveError):
            plan(base_spec(star={'teff': 3400}, exort_pkg='custom_src'))

    def test_missing_derived_inputs_path_is_an_error(self):
        reg = {**REGISTRY, 'paths': {k: v for k, v in REGISTRY['paths'].items()
                                      if k != 'derived_inputs'}}
        with self.assertRaises(derive_utils.DeriveError) as cm:
            plan(base_spec(star={'teff': 3400}), registry=reg)
        self.assertIn('derived_inputs', str(cm.exception))


class ApplyIntoSpec(unittest.TestCase):
    def test_fills_solar_and_ncdata_and_records_plan(self):
        spec = base_spec(star={'teff': 3400}, ic_template='ctrl40')
        warns, errs = build.apply_derived_inputs(spec, REGISTRY, REGISTRY['paths'])
        self.assertEqual((warns, errs), ([], []))
        self.assertTrue(spec['exo_solar_file'].endswith('btsettl_T3400_g4.5_m0.0_n68.nc'))
        self.assertTrue(spec['ncdata'].endswith('ic_ctrl40_2bar_L40_dry_ic.nc'))
        self.assertEqual(len(spec['_derived']), 3)
        self.assertTrue(spec['_albedo_product'].endswith('.albedos.sh'))
        # the ncdata path then resolves verbatim (absolute), not under the IC dir
        self.assertEqual(build.resolve_ic_path(spec['ncdata'], 'cam_aqua_fv', REGISTRY['paths']),
                         spec['ncdata'])
        # and passes the solar-file / exort_pkg stem check
        self.assertEqual([e for e in build.validate_case(spec, REGISTRY) if 'solar file' in e], [])

    def test_error_is_returned_not_raised(self):
        spec = base_spec(ic_template='nope')
        warns, errs = build.apply_derived_inputs(spec, REGISTRY, REGISTRY['paths'])
        self.assertEqual(len(errs), 1)
        self.assertIn('nope', errs[0])
        self.assertEqual(spec['_derived'], [])


class EmittedShell(unittest.TestCase):
    def setUp(self):
        self.spec = base_spec(star={'teff': 3400}, ic_template='ctrl40')
        build.apply_derived_inputs(self.spec, REGISTRY, REGISTRY['paths'])

    def test_required_inputs_block_guards_every_product(self):
        lines = build._build_required_inputs_block(self.spec, 'c1')
        guards = [l for l in lines if l.startswith('[ -f ')]
        self.assertEqual(len(guards), 3)
        for it in self.spec['_derived']:
            self.assertTrue(any(it.path in g for g in guards))
            self.assertTrue(any(it.summary in l for l in lines))
        self.assertTrue(all('exit 1' in g and 'build.py prep' in g for g in guards))

    def test_no_block_for_a_silent_matrix(self):
        spec = base_spec()
        build.apply_derived_inputs(spec, REGISTRY, REGISTRY['paths'])
        self.assertEqual(build._build_required_inputs_block(spec, 'c1'), [])
        self.assertEqual(build._build_derived_albedo_block(spec, 'c1'), [])

    def test_guard_fails_on_missing_product(self):
        script = '\n'.join(build._build_required_inputs_block(self.spec, 'c1'))
        r = subprocess.run(['bash', '-c', script], capture_output=True, text=True)
        self.assertEqual(r.returncode, 1)
        self.assertIn('missing derived solar input', r.stdout)

    def test_albedo_block_upserts_from_product_and_verifies(self):
        with tempfile.TemporaryDirectory() as d:
            product = os.path.join(d, 'p.albedos.sh')
            item = derive_utils.DerivedInput('albedo', product,
                                       {'solar_file': '/x/star.nc', 'ice_surface': 'blueice'}, 's')
            vals = {'albicev': 0.76, 'albicei': 0.195, 'albsnowv': 0.97, 'albsnowi': 0.47}
            with open(product, 'w') as f:
                f.write('\n'.join(derive_utils.albedo_product_lines(vals, item)) + '\n')
            self.assertEqual(derive_utils.read_albedo_product(product), vals)
            nl = os.path.join(d, 'user_nl_cice')
            with open(nl, 'w') as f:
                f.write("!! albedo settings\nalbicei = 0.30    !0.3\nalbicev = 0.67\n"
                        "albsnowi = 0.68\n albsnowv = 0.8\n!albicev = 0.1\nalbicev = 0.99\n")
            spec = dict(self.spec, _albedo_product=product)
            script = '\n'.join(build._build_derived_albedo_block(spec, 'c1'))
            r = subprocess.run(['bash', '-e', '-c', script], cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            text = open(nl).read()
            self.assertIn('albicev = 0.760000\n', text)
            self.assertIn('albsnowi = 0.470000\n', text)
            self.assertEqual(text.count('albicev ='), 2)      # one live + the commented one
            self.assertIn('!albicev = 0.1', text)              # comment preserved
            self.assertIn(f"{derive_utils.ALBEDO_PROVENANCE_PREFIX} {product}", text)
            self.assertEqual(derive_utils.albedo_product_from_nl(nl), product)
            # run it twice: still exactly one line per key, one provenance line
            r = subprocess.run(['bash', '-e', '-c', script], cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
            text = open(nl).read()
            self.assertEqual(text.count(derive_utils.ALBEDO_PROVENANCE_PREFIX), 1)

    def test_albedo_block_fails_if_product_defines_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            product = os.path.join(d, 'empty.sh')
            with open(product, 'w') as f:
                f.write('# nothing\n')
            with open(os.path.join(d, 'user_nl_cice'), 'w') as f:
                f.write('')
            spec = dict(self.spec, _albedo_product=product)
            script = '\n'.join(build._build_derived_albedo_block(spec, 'c1'))
            r = subprocess.run(['bash', '-c', script], cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 1)
            self.assertIn('ALBICEV not defined', r.stdout)


class RegistryRoundTrip(unittest.TestCase):
    """scan reads the manifests beside the products a case references; export
    turns them back into star / ic_template / ice_surface keys."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        derived = os.path.join(self.tmp, 'derived')
        registry = {**REGISTRY, 'paths': {**REGISTRY['paths'], 'derived_inputs': derived}}
        self.spec = base_spec(star={'teff': 3400}, ic_template='ctrl40')
        build.apply_derived_inputs(self.spec, registry, registry['paths'])
        # fake products + manifests (no engines)
        for it in self.spec['_derived']:
            os.makedirs(os.path.dirname(it.path), exist_ok=True)
            with open(it.path, 'w') as f:
                f.write('x')
            derive_utils.write_manifest(it, {}, {})
        # fake case dir
        case = os.path.join(self.tmp, 'case1')
        os.makedirs(os.path.join(case, 'SourceMods', 'src.share'))
        os.makedirs(os.path.join(case, 'SourceMods', 'src.cice'))
        with open(os.path.join(case, 'SourceMods', 'src.share', 'exoplanet_mod.F90'), 'w') as f:
            f.write("  real(r8), public, parameter :: exo_co2bar = 0.01\n"
                    f"  character(len=256), public, parameter :: exo_solar_file = '{self.spec['exo_solar_file']}'\n")
        with open(os.path.join(case, 'user_nl_cam'), 'w') as f:
            f.write(f"ncdata = '{self.spec['ncdata']}'\n")
        with open(os.path.join(case, 'user_nl_cice'), 'w') as f:
            f.write(f"{derive_utils.ALBEDO_PROVENANCE_PREFIX} {self.spec['_albedo_product']}\n"
                    "albicev = 0.76\nalbicei = 0.195\nalbsnowv = 0.97\nalbsnowi = 0.47\n")
        self.case = case

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_scan_recovers_provenance(self):
        row = scan.inspect_case(self.case)
        self.assertEqual((row['star_teff'], row['star_feh'], row['star_logg']), (3400.0, 0.0, 4.5))
        self.assertEqual(row['star_method'], 'log')
        self.assertEqual(row['ice_surface'], 'blueice')
        self.assertEqual(row['albedo_product'], self.spec['_albedo_product'])
        self.assertEqual((row['ic_template'], row['ic_dry'], row['ic_pstd_bar']), ('ctrl40', True, 2.0))
        self.assertEqual(row['cice_params']['albicev'], 0.76)
        for f in ('star_teff', 'ice_surface', 'ic_template'):
            self.assertIn(f, scan.REGISTRY_FIELDS)

    def test_export_emits_physics_not_products(self):
        row = scan.inspect_case(self.case)
        base = query._row_to_base(row)
        self.assertEqual(base['star'], {'teff': 3400.0, 'feh': 0.0, 'logg': 4.5})
        self.assertEqual(base['ic_template'], 'ctrl40')
        self.assertIs(base['ic_dry'], True)
        self.assertEqual(base['ice_surface'], 'blueice')
        self.assertIs(base['derive_albedos'], True)
        for k in ('exo_solar_file', 'ncdata', 'cice_params', 'star_teff', 'albedo_product'):
            self.assertNotIn(k, base)

    def test_hand_made_case_exports_as_before(self):
        # strip the provenance line and point at files with no manifest
        with open(os.path.join(self.case, 'user_nl_cice'), 'w') as f:
            f.write("albicev = 0.76\n")
        with open(os.path.join(self.case, 'user_nl_cam'), 'w') as f:
            f.write("ncdata = '/hand/made.nc'\n")
        row = scan.inspect_case(self.case)
        self.assertIsNone(row['ic_template'])
        self.assertIsNone(row['ice_surface'])
        base = query._row_to_base(row)
        self.assertEqual(base['ncdata'], '/hand/made.nc')
        self.assertEqual(base['cice_params'], {'albicev': 0.76})
        self.assertNotIn('ic_template', base)
        self.assertNotIn('derive_albedos', base)


class PrepCli(unittest.TestCase):
    def test_preview_lists_missing_and_exits_ok(self):
        with tempfile.TemporaryDirectory() as d:
            import yaml
            reg = {**REGISTRY, 'paths': {**REGISTRY['paths'], 'derived_inputs': os.path.join(d, 'derived')}}
            rp = os.path.join(d, 'reg.yaml')
            with open(rp, 'w') as f:
                yaml.safe_dump(reg, f)
            mp = os.path.join(d, 'm.yaml')
            mf = open(mp, 'w')
            yaml.safe_dump({'config_registry': rp,
                            'base': dict(base_spec(), mach='discover', stop_option='nyears', stop_n=1,
                                         rest_option='nyears', rest_n=1, resubmit=0, ntasks=1),
                            'cases': [{'name': 'a', 'star': {'teff': 3400}},
                                      {'name': 'b', 'ic_template': 'bogus'}]},
                           mf)
            mf.close()
            r = subprocess.run([sys.executable, os.path.join(ROOT, 'build.py'), 'prep', mp],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 1, r.stdout)       # case b is a matrix error
            self.assertIn('[MISSING] solar', r.stdout)
            self.assertIn('[MISSING] albedo', r.stdout)
            self.assertIn('preview only', r.stdout)
            r = subprocess.run([sys.executable, os.path.join(ROOT, 'build.py'), 'prep', mp, '--case', 'a'],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout)
            self.assertIn('2 distinct product(s): 0 present, 2 missing', r.stdout)


@unittest.skipUnless(HAVE_ENGINES, 'local ExoCAM/ExoRT checkouts not present')
class Engines(unittest.TestCase):
    """The science acceptance tests, run only where the inputs live."""

    def test_pressure_rescale_reproduces_idl_products(self):
        sys.path.insert(0, f'{EXOCAM}/tools/py_progs')
        import netCDF4 as nc
        import numpy as np
        import mod_ncdata
        with tempfile.TemporaryDirectory() as d:
            for pstd, dry, ref in ((2.0, False, 'ic_2bar_L40_ic.nc'),
                                   (4.0, True, 'ic_4bar_L40_dry_ic.nc')):
                out = os.path.join(d, ref)
                mod_ncdata.modify_ncdata(f'{IC_DIR}/control_L40.cam.i.0009-01-01-00000.nc', out,
                                         nlev=40, pstd_bar=pstd, dry=dry, verbose=False)
                A, B = nc.Dataset(out), nc.Dataset(f'{IC_DIR}/{ref}')
                for v in A.variables:
                    x, y = np.array(A[v][:]), np.array(B[v][:])
                    if x.dtype.kind in 'SU':
                        continue
                    self.assertEqual(x.shape, y.shape, v)
                    self.assertTrue(np.array_equal(x, y), f'{ref}: {v} differs')
                A.close(); B.close()

    def test_albedo_calculator_reference_values(self):
        sys.path.insert(0, f'{EXOCAM}/tools/py_progs')
        from broadband_albedo_calculator import compute_cice_albedos
        r = compute_cice_albedos(f'{EXORT}/data/solar/G2V_SUN_n68.nc', 'blueice')
        self.assertAlmostEqual(r['albicev'], 0.8160, places=3)
        self.assertAlmostEqual(r['albicei'], 0.2527, places=3)
        self.assertAlmostEqual(r['albsnowv'], 0.9816, places=3)
        self.assertAlmostEqual(r['albsnowi'], 0.5866, places=3)

    def test_derive_ic_and_albedo_engines_via_derive(self):
        with tempfile.TemporaryDirectory() as d:
            paths = {'exocam_root': EXOCAM, 'exort_root': EXORT, 'derived_inputs': d}
            reg = {'paths': paths, 'ic_templates': REGISTRY['ic_templates']}
            spec = base_spec(star='sun', ic_template='ctrl40')
            build.apply_derived_inputs(spec, reg, paths)
            for it in spec['_derived']:
                derive_utils.derive(it, paths, verbose=False)
                self.assertTrue(os.path.isfile(it.path))
                m = derive_utils.read_manifest(it.path)
                self.assertEqual(m['kind'], it.kind)
            alb = derive_utils.read_albedo_product(spec['_albedo_product'])
            self.assertAlmostEqual(alb['albicev'], 0.8160, places=3)


if __name__ == '__main__':
    unittest.main(verbosity=1)
