import asyncio
import time
from collections import defaultdict
from ipaddress import ip_network
from typing import Coroutine, Dict, List, Optional

from aggregate6 import aggregate
from databases import Database

from irrexplorer.api.interfaces import (
    ASNPrefixes,
    MemberOf,
    PrefixIRRDetail,
    PrefixSummary,
    SetExpansion,
)
from irrexplorer.backends.bgp import BGPQuery
from irrexplorer.backends.irrd import IRRDQuery
from irrexplorer.backends.rirstats import RIRStatsQuery
from irrexplorer.settings import MINIMUM_PREFIX_SIZE, TESTING
from irrexplorer.state import RIR, IPNetwork, RouteInfo, NIR
from irrexplorer.api.interfaces import ObjectClass

SET_SIZE_LIMIT = 1000
NIR_NAMES = [nir.name for nir in NIR]


class PrefixCollector:
    """
    Collect data about a particular prefix.

    Given a search prefix, call prefix_summary() to get a list
    of PrefixSummary objects, each of which contains all the info
    about one prefix.
    """

    def __init__(self, database: Database):
        self.database = database
        self.rirstats: List[RouteInfo] = []
        self.routes_irrd: Dict[IPNetwork, List[RouteInfo]] = {}
        self.routes_bgp: Dict[IPNetwork, List[RouteInfo]] = {}

    async def prefix_summary(self, search_prefix: IPNetwork) -> List[PrefixSummary]:
        # This check should be caught by clean_query in normal use, this is
        # merely an additional safety.
        if MINIMUM_PREFIX_SIZE[search_prefix.version] > search_prefix.prefixlen:
            return []

        start = time.perf_counter()

        await self._collect_for_prefixes([search_prefix])
        prefix_summaries = self._collate_per_prefix()
        print(f"complete in {time.perf_counter() - start}")
        return prefix_summaries

    async def asn_summary(self, asn: int) -> ASNPrefixes:
        start = time.perf_counter()

        aggregates = await self._collect_aggregate_prefixes_for_asn(asn)
        await self._collect_for_prefixes(aggregates)
        prefix_summaries = self._collate_per_prefix()
        response = ASNPrefixes()
        for p in prefix_summaries:
            if asn in p.bgp_origins or asn in p.rpki_origins or asn in p.irr_origins:
                response.direct_origin.append(p)
            else:
                response.overlaps.append(p)
        print(f"complete in {time.perf_counter() - start}")
        return response

    async def _collect_for_prefixes(self, search_prefixes: List[IPNetwork]) -> None:
        """
        Collect all relevant data for `search_prefix` from remote systems,
        and set the results into self.irrd_per_prefix,
        self.bgp_per_prefix, self.aggregates and self.rirstats.
        """
        self.irrd_per_prefix = defaultdict(list)
        self.bgp_per_prefix = defaultdict(list)

        if not search_prefixes:
            return

        tasks = [
            IRRDQuery().query_prefixes_any(search_prefixes),
            BGPQuery(self.database).query_prefixes_any(search_prefixes),
            RIRStatsQuery(self.database).query_prefixes_any(search_prefixes),
        ]
        routes_irrd, routes_bgp, self.rirstats = await _execute_tasks(tasks)

        for result in routes_irrd:
            self.irrd_per_prefix[result.prefix].append(result)

        for result in routes_bgp:
            self.bgp_per_prefix[result.prefix].append(result)

        self.aggregates = ip_networks_aggregates(
            list(self.irrd_per_prefix.keys()) + list(self.bgp_per_prefix.keys())
        )

    async def _collect_aggregate_prefixes_for_asn(self, asn: int) -> List[IPNetwork]:
        """ """
        tasks = [
            IRRDQuery().query_asn(asn),
            BGPQuery(self.database).query_asn(asn),
        ]
        routes_irrd, routes_bgp = await _execute_tasks(tasks)
        return ip_networks_aggregates(
            [
                route.prefix
                for route in routes_irrd + routes_bgp
                if MINIMUM_PREFIX_SIZE[route.prefix.version] <= route.prefix.prefixlen
            ]
        )

    def _collate_per_prefix(self) -> List[PrefixSummary]:
        """
        Collate the data per prefix into a list of PrefixSummary objects.
        Translates the output from _collect into a list of PrefixSummary objects,
        one per unique prefix found, with the RIR, BGP origins, and IRR routes set.
        """
        all_prefixes = set(self.irrd_per_prefix.keys()).union(set(self.bgp_per_prefix.keys()))
        summaries_per_prefix = []
        for prefix in all_prefixes:
            rir = self._rir_for_prefix(prefix)

            bgp_origins = {r.asn for r in self.bgp_per_prefix.get(prefix, []) if r.asn}
            summary = PrefixSummary(prefix=prefix, rir=rir, bgp_origins=bgp_origins)

            if prefix in self.irrd_per_prefix:
                irr_entries = self.irrd_per_prefix[prefix]
                irr_entries.sort(key=lambda r: r.asn if r.asn else 0)
                for entry in irr_entries:
                    assert entry.asn is not None, entry
                    assert entry.irr_source, entry
                    if entry.irr_source == "RPKI":
                        target = summary.rpki_routes
                    else:
                        target = summary.irr_routes[entry.irr_source]
                    target.append(
                        PrefixIRRDetail(
                            asn=entry.asn,
                            rpsl_pk=entry.rpsl_pk,
                            rpki_status=entry.rpki_status,
                            rpki_max_length=entry.rpki_max_length,
                            rpsl_text=entry.rpsl_text,
                        )
                    )
            summaries_per_prefix.append(summary)
        return summaries_per_prefix

    def _rir_for_prefix(self, prefix: IPNetwork) -> Optional[RIR]:
        """
        Find the responsible RIR/NIR for a prefix, from self.rirstats previously
        gathered by _collect(), and prefer NIR over RIR.
        """
        relevant_rirstat = None

        for rirstat in self.rirstats:
            if rirstat.prefix.overlaps(prefix):
                relevant_rirstat = rirstat
                if rirstat.rir and rirstat.rir.name in NIR_NAMES:
                    # Break early if this is a NIR, as those take priority
                    break
        return relevant_rirstat.rir if relevant_rirstat else None


async def collect_member_of(target: str, object_class: ObjectClass) -> MemberOf:
    start = time.perf_counter()
    result = MemberOf()
    data = await IRRDQuery().query_member_of(target, object_class)
    irrs_seen = set()

    for found_set in data["set"]:
        irrs_seen.add(found_set["source"])
        result.sets_per_irr[found_set["source"]].add(found_set["rpslPk"])

    if object_class == ObjectClass.ASSET:
        for autnum in data.get("autNum", []):
            autnum_mntners = set(autnum["mntBy"])
            for member_of in autnum["memberOfObjs"]:
                expected_mntners = set()
                if member_of and member_of.get("mbrsByRef"):
                    expected_mntners = set(member_of["mbrsByRef"])

                if "ANY" in expected_mntners or autnum_mntners.intersection(expected_mntners):
                    irrs_seen.add(member_of["source"])
                    result.sets_per_irr[member_of["source"]].add(member_of["rpslPk"])

    result.irrs_seen = sorted(irrs_seen)
    print(f"complete in {time.perf_counter() - start}")
    return result


async def collect_set_expansion(name: str):
    def is_set(set_name: str) -> bool:
        return set_name[:2] != "AS" or not set_name[2:].isnumeric()

    start = time.perf_counter()
    irrd = IRRDQuery()

    resolved: Dict[str, Dict[str, List[str]]] = {name: {}}
    to_resolve = {name}
    tree_depth = 0

    while to_resolve:
        tree_depth += 1
        print(
            f"starting step {tree_depth} with {len(to_resolve)} items to resolve, {len(resolved)} already done"
        )
        if len(to_resolve) > SET_SIZE_LIMIT or len(resolved) > SET_SIZE_LIMIT:
            print("breaking")
            break
        step_result = await irrd.query_set_members(list(to_resolve))
        resolved.update(step_result)
        to_resolve = {
            member
            for members_per_source in step_result.values()
            for members in members_per_source.values()
            for member in members
            if is_set(member) and member not in to_resolve
        }
        to_resolve = to_resolve - set(resolved.keys())

    results = []

    def traverse_tree(stub_name: str, depth: int = 0, path: Optional[List[str]] = None) -> None:
        print(f"traverse_tree called with: stub_name={stub_name} depth={depth} path={path}")
        if path is None:
            path = []
        if stub_name in path:
            return  # circular reference
        path = path + [stub_name]
        depth += 1
        for source, members in resolved[stub_name].items():
            result = SetExpansion(
                name=stub_name, source=source, depth=depth, path=path, members=sorted(members)
            )
            if result not in results:
                results.append(result)
        for sub_members in resolved[stub_name].values():
            for sub_member in sub_members:
                if sub_member in resolved:
                    traverse_tree(sub_member, depth, path)

    print("completed initial resolve loop, traversing tree")
    traverse_tree(name)
    results.sort(key=lambda item: (item.depth, item.name))

    print(f"set expansion complete in {time.perf_counter() - start}")
    return results


async def _execute_tasks(tasks: List[Coroutine]):
    # force_rollback, used in tests, has issues with executing the tasks
    # concurrently - therefore, in testing, they're executed sequentially
    if TESTING:
        return [await t for t in tasks]
    else:  # pragma: no cover
        return await asyncio.gather(*tasks)


def ip_networks_aggregates(prefixes: List[IPNetwork]):
    inputs = [str(prefix) for prefix in prefixes]
    return [ip_network(prefix) for prefix in aggregate(inputs)]
