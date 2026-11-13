import concurrent.futures
import json
import math
import random
import sqlite3
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path
from spatial import Store, Conflict, Missing, EARTH_KM, distance, point, SAMPLES
from run import make_server


def p(name,lat=0,lon=0):
    return {'name':name,'latitude':lat,'longitude':lon}


class SpatialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)/'points.sqlite3'
        self.store = Store(self.path)

    def tearDown(self):
        self.temp.cleanup()

    def test_known_spherical_distances(self):
        self.assertAlmostEqual(distance(0,0,0,90),EARTH_KM*math.pi/2)
        self.assertAlmostEqual(distance(0,0,0,180),EARTH_KM*math.pi)
        self.assertAlmostEqual(distance(90,-180,90,25),0,places=8)

    def test_dateline_radius_splits_boxes_without_losing_neighbors(self):
        self.store.add([p('west',-17,179.4),p('east',-17,-179.5),p('far',-17,160)])
        result=self.store.search(-17,179.9,100)
        self.assertEqual(len(result['bounds']),2)
        self.assertEqual({q['name'] for q in result['matches']},{'west','east'})
        self.assertEqual(result['candidates'],2)

    def test_pole_queries_span_all_longitudes(self):
        self.store.add([p('a',89.5,80),p('b',89.5,-100),p('far',85,0)])
        result=self.store.search(90,0,60)
        self.assertEqual(len(result['matches']),2)
        self.assertEqual(result['bounds'][0]['west'],-180)
        self.assertEqual(result['bounds'][0]['east'],180)

    def test_zero_radius_boundary_and_world_search(self):
        self.store.add([p('date',0,180),p('quarter',0,90),p('origin',0,0)])
        self.assertEqual(self.store.list()[0]['longitude'],-180)
        self.assertEqual([r['name'] for r in self.store.search(0,-180,0)['matches']],['date'])
        self.assertEqual(len(self.store.search(0,0,math.pi*EARTH_KM/2)['matches']),2)
        self.assertEqual(len(self.store.search(-80,31,20100)['matches']),3)

    def test_indexed_search_matches_independent_vector_angle_scan(self):
        rng=random.Random(317)
        points=[p(str(i),rng.uniform(-90,90),rng.uniform(-180,180)) for i in range(500)]
        points.extend([p('north',90,80),p('south',-90,-80),p('dateline',0,180)])
        self.store.add(points[:500]);self.store.add(points[500:])
        records=self.store.list()
        def vector(lat,lon):
            a,b=math.radians(lat),math.radians(lon)
            return (math.cos(a)*math.cos(b),math.cos(a)*math.sin(b),math.sin(a))
        for trial in range(180):
            lat=rng.uniform(-90,90);lon=rng.uniform(-180,180);radius=rng.uniform(0,20100)
            if trial<10:
                lat,lon,radius=[(90,0,100),(-90,0,100),(0,180,100),(0,-180,100),(0,0,20100)][trial%5]
            center=vector(lat,lon);expected=set()
            for row in records:
                other=vector(row['latitude'],row['longitude'])
                cross=(center[1]*other[2]-center[2]*other[1],center[2]*other[0]-center[0]*other[2],center[0]*other[1]-center[1]*other[0])
                km=EARTH_KM*math.atan2(math.sqrt(sum(x*x for x in cross)),sum(a*b for a,b in zip(center,other)))
                if km<=radius+1e-8:expected.add(row['id'])
            result=self.store.search(lat,lon,radius)
            self.assertEqual({row['id'] for row in result['matches']},expected)
            self.assertEqual(result['matches'],sorted(result['matches'],key=lambda row:(row['distance_km'],row['id'])))

    def test_updates_archive_restore_and_persistence_keep_index_consistent(self):
        identity=self.store.add([p('moving')])[0]
        changed=self.store.update(identity,1,values=p('moving',45,45))
        self.assertEqual(changed['version'],2)
        self.assertEqual(self.store.search(0,0,1)['matches'],[])
        self.assertEqual(len(self.store.search(45,45,0)['matches']),1)
        self.store.update(identity,2,active=False)
        self.assertEqual(self.store.search(45,45,1)['total'],0)
        self.store.update(identity,3,active=True)
        reopened=Store(self.path)
        self.assertEqual(len(reopened.search(45,45,0)['matches']),1)
        with reopened.connection() as db:
            self.assertEqual(db.execute("SELECT rtreecheck('point_index')").fetchone()[0],'ok')

    def test_optimistic_versions_allow_only_one_concurrent_writer(self):
        identity=self.store.add([p('initial')])[0]
        barrier=threading.Barrier(2)
        def change(name):
            barrier.wait()
            try:
                self.store.update(identity,1,values=p(name));return 'saved'
            except Conflict:
                return 'conflict'
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures=[pool.submit(change,name) for name in ('one','two')]
            self.assertCountEqual([f.result() for f in futures],['saved','conflict'])
        self.assertEqual(self.store.list()[0]['version'],2)

    def test_batch_validation_and_mid_transaction_failure_roll_back_all_rows(self):
        with self.assertRaises(ValueError):self.store.add([p('ok'),p('bad',91,0)])
        self.assertEqual(self.store.list(),[])
        with self.store.connection() as db:
            db.execute("CREATE TRIGGER reject_test BEFORE INSERT ON points WHEN new.name='reject' BEGIN SELECT RAISE(ABORT,'test rejection'); END")
        with self.assertRaises(sqlite3.IntegrityError):self.store.add([p('first'),p('reject')])
        self.assertEqual(self.store.list(),[])
        with self.store.connection() as db:self.assertEqual(db.execute('SELECT count(*) FROM point_index').fetchone()[0],0)

    def test_capacity_includes_archived_points_and_rejects_extra_batch(self):
        for batch in range(4):self.store.add([p(f'{batch}-{i}') for i in range(500)])
        self.store.update(1,1,active=False)
        with self.assertRaises(ValueError):self.store.add([p('overflow')])
        self.assertEqual(len(self.store.list()),2000)

    def test_strict_validation_and_missing_or_stale_records(self):
        for bad in [p('',0,0),p('x',float('nan'),0),p('x',True,0),p('x',0,181),{'name':'x'},p('x\n',0,0)]:
            with self.assertRaises(ValueError):point(bad)
        for radius in [float('inf'),-1,20101,True]:
            with self.assertRaises(ValueError):self.store.search(0,0,radius)
        with self.assertRaises(Missing):self.store.update(99,1,active=False)
        identity=self.store.add([p('x')])[0]
        with self.assertRaises(Conflict):self.store.update(identity,2,active=False)


class APITests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.server=make_server(Path(self.temp.name)/'db.sqlite3')
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def request(self,method,path,body=None,headers=None):
        connection=HTTPConnection(*self.server.server_address,timeout=3)
        request_headers={'Content-Type':'application/json',**(headers or {})}
        connection.request(method,path,json.dumps(body) if body is not None else None,request_headers)
        response=connection.getresponse();raw=response.read();status=response.status;connection.close()
        return status,json.loads(raw) if raw and response.headers['Content-Type'].startswith('application/json') else raw

    def test_crud_search_conflict_and_archive_api(self):
        status,data=self.request('POST','/api/points',{'points':SAMPLES});self.assertEqual(status,201)
        status,data=self.request('POST','/api/search',{'latitude':-17,'longitude':179.9,'radius':100});self.assertEqual(status,200);self.assertEqual(len(data['matches']),2)
        self.assertEqual(self.request('POST','/api/points/1/edit',{'version':1,'point':p('changed')})[0],200)
        self.assertEqual(self.request('POST','/api/points/1/archive',{'version':1})[0],409)
        self.assertEqual(self.request('POST','/api/points/1/archive',{'version':2})[0],200)
        self.assertEqual(self.request('POST','/api/points/1/restore',{'version':3})[0],200)

    def test_boundary_headers_content_types_and_private_paths(self):
        self.assertEqual(self.request('GET','/api/points',headers={'Host':'attacker.invalid'})[0],400)
        self.assertEqual(self.request('POST','/api/search',{},headers={'Origin':'https://attacker.invalid'})[0],403)
        self.assertEqual(self.request('POST','/api/search',{},headers={'Content-Type':'text/plain'})[0],415)
        self.assertEqual(self.request('POST','/api/search',{'latitude':0,'longitude':0,'radius':1,'extra':1})[0],400)
        for path in ['/data/points.sqlite3','/.git/config','/spatial.py']:
            self.assertEqual(self.request('GET',path)[0],404)
        self.assertEqual(self.request('PUT','/api/points',{})[0],405)
