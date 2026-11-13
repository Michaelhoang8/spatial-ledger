"""Persistent point records and conservative spatial candidates, refined on a sphere."""
import math
import sqlite3
from contextlib import contextmanager
from pathlib import Path

EARTH_KM = 6371.0088
MAX_POINTS = 2000


class Conflict(ValueError):
    pass


class Missing(ValueError):
    pass


def number(value, low, high, label):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f'{label} must be a finite number from {low} to {high}.')
    return float(value)


def longitude(value):
    value = number(value, -180, 180, 'Longitude')
    return -180.0 if value == 180 else value


def point(value):
    if not isinstance(value, dict) or set(value) != {'name', 'latitude', 'longitude'}:
        raise ValueError('Each point needs name, latitude, and longitude only.')
    name = value['name']
    if not isinstance(name, str) or not name.strip() or len(name) > 80 or any(ord(c) < 32 for c in name):
        raise ValueError('Point names must contain 1–80 printable characters.')
    return {'name': name.strip(), 'latitude': number(value['latitude'], -90, 90, 'Latitude'), 'longitude': longitude(value['longitude'])}


def distance(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2-lon1)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(dl/2)**2
    return 2*EARTH_KM*math.atan2(math.sqrt(max(0, min(1, h))), math.sqrt(max(0, 1-h)))


def bounds(latitude, lon, radius):
    # Expand by the same small distance tolerance used in the final predicate.
    theta = (radius+1e-8)/EARTH_KM
    phi = math.radians(latitude)
    if theta >= math.pi:
        return [{'south': -90, 'north': 90, 'west': -180, 'east': 180}]
    south, north = max(-90, latitude-math.degrees(theta)), min(90, latitude+math.degrees(theta))
    if south <= -90 or north >= 90:
        spans = [(-180, 180)]
    else:
        delta = math.degrees(math.asin(min(1, math.sin(theta)/math.cos(phi)))) + 1e-9
        west, east = lon-delta, lon+delta
        if west < -180:
            spans = [(-180, east), (west+360, 180)]
        elif east > 180:
            spans = [(west, 180), (-180, east-360)]
        else:
            spans = [(west, east)]
    return [{'south': max(-90, south-1e-9), 'north': min(90, north+1e-9), 'west': w, 'east': e} for w, e in spans]


class Store:
    def __init__(self, path):
        self.path = str(path)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS points(
                    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
                    latitude REAL NOT NULL, longitude REAL NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1);
                CREATE VIRTUAL TABLE IF NOT EXISTS point_index USING rtree(id, west, east, south, north);
                CREATE TRIGGER IF NOT EXISTS points_insert AFTER INSERT ON points WHEN new.active=1 BEGIN
                    INSERT INTO point_index VALUES(new.id,new.longitude,new.longitude,new.latitude,new.latitude);
                END;
                CREATE TRIGGER IF NOT EXISTS points_update AFTER UPDATE ON points BEGIN
                    DELETE FROM point_index WHERE id=old.id;
                    INSERT INTO point_index SELECT new.id,new.longitude,new.longitude,new.latitude,new.latitude WHERE new.active=1;
                END;
            ''')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def list(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute('SELECT * FROM points ORDER BY id')]

    def add(self, items):
        if not isinstance(items, list) or not 1 <= len(items) <= 500:
            raise ValueError('Import between 1 and 500 points at a time.')
        clean = [point(item) for item in items]
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            count = db.execute('SELECT count(*) FROM points').fetchone()[0]
            if count+len(clean) > MAX_POINTS:
                raise ValueError('The ledger holds at most 2,000 points, including archived records.')
            ids = []
            for item in clean:
                cursor = db.execute('INSERT INTO points(name,latitude,longitude) VALUES(?,?,?)', tuple(item.values()))
                ids.append(cursor.lastrowid)
            return ids

    def update(self, identity, version, values=None, active=None):
        if type(identity) is not int or identity < 1 or type(version) is not int or version < 1:
            raise ValueError('ID and version must be positive integers.')
        if (values is None) == (active is None) or (active is not None and type(active) is not bool):
            raise ValueError('Choose either point edits or an archive/restore operation.')
        clean = point(values) if values is not None else None
        with self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT * FROM points WHERE id=?', (identity,)).fetchone()
            if not old:
                raise Missing('Point not found.')
            if old['version'] != version:
                raise Conflict('This point changed elsewhere. Refresh before saving.')
            if clean:
                db.execute('UPDATE points SET name=?,latitude=?,longitude=?,version=version+1 WHERE id=?', (*clean.values(), identity))
            else:
                db.execute('UPDATE points SET active=?,version=version+1 WHERE id=?', (int(active), identity))
            return dict(db.execute('SELECT * FROM points WHERE id=?', (identity,)).fetchone())

    def search(self, latitude, lon, radius):
        latitude = number(latitude, -90, 90, 'Latitude')
        lon = longitude(lon)
        radius = number(radius, 0, 20100, 'Radius')
        boxes = bounds(latitude, lon, radius)
        candidates = {}
        with self.connection() as db:
            db.execute('BEGIN')
            total = db.execute('SELECT count(*) FROM points WHERE active=1').fetchone()[0]
            for box in boxes:
                rows = db.execute('''SELECT p.* FROM point_index r JOIN points p ON p.id=r.id
                    WHERE r.east>=? AND r.west<=? AND r.north>=? AND r.south<=? AND p.active=1''',
                    (box['west'],box['east'],box['south'],box['north'])).fetchall()
                candidates.update((r['id'], dict(r)) for r in rows)
        matches = []
        for item in candidates.values():
            km = distance(latitude, lon, item['latitude'], item['longitude'])
            if km <= radius+1e-8:
                matches.append({**item, 'distance_km': km})
        matches.sort(key=lambda p: (p['distance_km'], p['id']))
        return {'center': {'latitude': latitude, 'longitude': lon}, 'radius_km': radius,
                'bounds': boxes, 'total': total, 'candidates': len(candidates), 'matches': matches,
                'model': f'Spherical great-circle distance; radius {EARTH_KM} km; tolerance 1e-8 km.'}


SAMPLES = [
    {'name': 'Alder station', 'latitude': 51.2, 'longitude': -0.3},
    {'name': 'Birch station', 'latitude': 52.1, 'longitude': 1.4},
    {'name': 'Cedar station', 'latitude': 48.8, 'longitude': 2.3},
    {'name': 'Date line west', 'latitude': -17, 'longitude': 179.4},
    {'name': 'Date line east', 'latitude': -17, 'longitude': -179.5},
    {'name': 'Polar relay A', 'latitude': 89.5, 'longitude': 80},
    {'name': 'Polar relay B', 'latitude': 89.5, 'longitude': -100},
    {'name': 'Southern relay', 'latitude': -44, 'longitude': 170},
]
