"""Offline country/city matching using GeoNames; no external geocoding calls."""
from functools import lru_cache
import re
import unicodedata
from geonamescache import GeonamesCache


def normalized(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD',value.casefold()) if not unicodedata.combining(c)).split())


@lru_cache(maxsize=1)
def geography():
    """Build one bounded dataset index per process, outside per-row SQL work."""
    source=GeonamesCache(min_city_population=500)
    countries={}
    for code,country in source.get_countries().items():
        for alias in (code,country['iso3'],country['name']):
            countries[normalized(alias)]=code
    for alias,code in {'uk':'GB','usa':'US','u.s.a.':'US','u.s.':'US','uae':'AE','south korea':'KR','north korea':'KP','russia':'RU','vietnam':'VN','czech republic':'CZ','turkey':'TR','bharat':'IN'}.items():
        countries[alias]=code
    cities={}
    city_countries={}
    us_state_cities={}
    for identifier,city in source.get_cities().items():
        identifier=int(identifier)
        city_countries[identifier]=city['countrycode']
        if city['countrycode']=='US':
            us_state_cities.setdefault(city['admin1code'].casefold(),set()).add(identifier)
        for alias in (city['name'],*city['alternatenames']):
            name=normalized(alias)
            if name and (len(name)>=3 or alias==city['name']):
                previous=cities.get(name)
                if previous is None:
                    cities[name]=identifier
                elif isinstance(previous,int):
                    if previous!=identifier:
                        cities[name]=(previous,identifier)
                elif identifier not in previous:
                    cities[name]=(*previous,identifier)
    regions={normalized(state['name']):'US' for state in source.get_us_states().values()}
    for name in INDIAN_STATES:
        regions[name]='IN'
    return countries,cities,city_countries,regions,us_state_cities


INDIAN_STATES=frozenset("""andhra pradesh|arunachal pradesh|assam|bihar|chhattisgarh|goa|gujarat|haryana|himachal pradesh|jharkhand|karnataka|kerala|madhya pradesh|maharashtra|manipur|meghalaya|mizoram|nagaland|odisha|orissa|punjab|rajasthan|sikkim|tamil nadu|telangana|tripura|uttar pradesh|uttarakhand|west bengal|andaman and nicobar|chandigarh|dadra and nagar haveli|daman and diu|delhi|new delhi|jammu and kashmir|ladakh|lakshadweep|puducherry|pondicherry""".split('|'))


def city_ids(cities, name):
    value=cities.get(name)
    return () if value is None else (value,) if isinstance(value,int) else value


def places(value):
    countries,cities,city_countries,regions,us_state_cities=geography()
    parts=[normalized(p) for p in re.split(r'[,/|()]|\s+-\s+',value)]
    parts=[re.sub(r'^(?:remote|hybrid|onsite|on-site)\s*[-:]\s*','',p) for p in parts]
    explicit={countries[p] for p in parts if p in countries}
    ids=set().union(*(city_ids(cities,p) for p in parts))
    for part in parts:
        if part in us_state_cities and ids & us_state_cities[part]:
            explicit.discard(countries.get(part))
            explicit.add('US')
    if explicit:
        ids={identifier for identifier in ids if city_countries[identifier] in explicit}
        return explicit,ids
    inferred={regions[p] for p in parts if p in regions}
    for part in parts:
        candidates={city_countries[i] for i in city_ids(cities,part)}
        if len(candidates)==1:
            inferred.update(candidates)
    return inferred,ids


def matches(location, query):
    """Match countries through unambiguous cities, and city aliases through IDs.

    Semicolons separate offices with independent country context. Short country
    codes must be standalone segments. Remote-only/unknown values imply no country.
    """
    if not isinstance(location,str) or not isinstance(query,str):
        return False
    location,query=location.strip(),query.strip()
    if not query:
        return True
    countries,cities,city_countries,regions,us_state_cities=geography()
    wanted=normalized(query)
    query_countries,query_ids=places(query)
    for office in location.split(';'):
        office_countries,office_ids=places(office)
        if wanted in countries:
            if countries[wanted] in office_countries:
                return True
        elif query_ids:
            common=query_ids & office_ids
            if common and (not query_countries or not office_countries or query_countries & office_countries):
                return True
        elif wanted in regions:
            if wanted in normalized(office) and regions[wanted] in office_countries:
                return True
        elif wanted in normalized(office):
            return True
    return False
