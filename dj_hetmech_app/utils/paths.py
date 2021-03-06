import collections
import logging

from django.db.models import Q
import hetnetpy.neo4j

from dj_hetmech_app.utils import (
    get_hetionet_metagraph,
    get_neo4j_driver,
)


cypher_degree_query = '''\
MATCH (node)-[rel]-()
WHERE id(node) = $node_id
  AND type(rel) = $rel_type
RETURN
  id(node) AS node_id,
  type(rel) AS rel_type,
  count(rel) AS degree
'''


def get_node_degree(node_id, rel_type):
    """Get a node degree for a given neo4j node ID and relationship type."""
    from hetnetpy.hetnet import MetaEdge
    if isinstance(rel_type, MetaEdge):
        rel_type = rel_type.neo4j_rel_type
    driver = get_neo4j_driver()
    with driver.session() as session:
        results = session.run(cypher_degree_query, node_id=node_id, rel_type=rel_type)
        result = results.single()
    return result['degree'] if result else 0


def get_pathcount_record(metapath, source_id, target_id, path_count, raw_dwpc):
    """
    Return the record from the PathCount table for a given metapath, source node,
    and target node. If the record does not exist in the PathCount table, check
    whether the DegreeGroupedPermutation table contains the corresponding null DWPC
    information and use raw_dwpc to create a PathCount record on the fly. If no
    null DWPC information exists, return None.
    """
    from dj_hetmech_app.models import DegreeGroupedPermutation, Metapath, Node, PathCount

    # Return the PathCount record if it is stored in the database 
    pathcounts_qs = PathCount.objects.filter(
        Q(metapath=metapath.abbrev, source=source_id, target=target_id) |
        Q(metapath=metapath.inverse.abbrev, source=target_id, target=source_id)
    )
    pathcount_record = pathcounts_qs.first()
    pathcounts_qs_count = pathcounts_qs.count()
    if pathcounts_qs_count > 1:
        # see https://github.com/greenelab/connectivity-search-backend/issues/43
        import pandas
        qs_df = pandas.DataFrame.from_records(pathcounts_qs.all().values())
        logging.warning(
            f'get_paths returned {pathcounts_qs_count} results, '
            'but database should not have more than one row (including inverse orientation) for '
            f'{metapath.abbrev} from {source_id} to {target_id}.\n'
            + qs_df.to_string(index=False)
        )
    if pathcount_record:
        pathcount_record.reversed = pathcount_record.metapath.abbreviation != metapath.abbrev
        return pathcount_record

    # Compute the PathCount record on-the-fly 
    metapath_record = get_metapath_instance(metapath)
    if not metapath_record:
        return None
    # Reorient metapath according to database orientation
    metapath_record.reversed = metapath_record.abbreviation != metapath.abbrev
    if metapath_record.reversed:
        metapath = metapath.inverse
        source_id, target_id = target_id, source_id
        assert metapath_record.abbreviation == metapath.abbrev
    source_degree = get_node_degree(source_id, metapath[0])
    target_degree = get_node_degree(target_id, metapath[-1])
    import numpy
    dwpc = numpy.arcsinh(raw_dwpc / metapath_record.dwpc_raw_mean)
    dgp_record = DegreeGroupedPermutation.objects.get(
        metapath=metapath_record, source_degree=source_degree, target_degree=target_degree)
    hetmatpy_info = {
        'dwpc': dwpc,
        'n': dgp_record.n_dwpcs,
        'nnz': dgp_record.n_nonzero_dwpcs,
        'mean_nz': dgp_record.nonzero_mean,
        'sd_nz': dgp_record.nonzero_sd,
    }
    from hetmatpy.pipeline import calculate_p_value
    p_value = calculate_p_value(hetmatpy_info)
    pathcount_record = PathCount(
        metapath=metapath_record,
        source=Node.objects.get(pk=source_id),
        target=Node.objects.get(pk=target_id),
        dgp=dgp_record,
        path_count=path_count,
        dwpc=dwpc,
        p_value=p_value,
    )
    pathcount_record.reversed = metapath_record.reversed
    return pathcount_record


def get_paths(metapath, source_id, target_id, limit=None):
    """
    Return JSON-serializeable object with paths between two nodes for a given metapath.
    """
    metagraph = get_hetionet_metagraph()
    metapath = metagraph.get_metapath(metapath)

    from dj_hetmech_app.models import Node
    source_record = Node.objects.get(pk=source_id)
    target_record = Node.objects.get(pk=target_id)
    source_identifier = source_record.get_cast_identifier()
    target_identifier = target_record.get_cast_identifier()

    query = hetnetpy.neo4j.construct_pdp_query(
        metapath, property='identifier', path_style='id_lists', aggregate_columns=True)
    if limit is not None:
        query += f'\nLIMIT {limit}'
    driver = get_neo4j_driver()
    neo4j_params = {
        'source': source_identifier,
        'target': target_identifier,
        'w': 0.5,
    }
    with driver.session() as session:
        results = session.run(query, neo4j_params)
        results = [dict(record) for record in results]

    metapath_score = None
    pathcount_record = get_pathcount_record(
        metapath, source_id, target_id,
        path_count=results[0].pop('PC') if results else 0,
        raw_dwpc=results[0].pop('DWPC') if results else 0.0,
    )
    if pathcount_record:
        import math
        adj_p_value = pathcount_record.get_adjusted_p_value()
        metapath_score = -math.log10(adj_p_value)

    neo4j_node_ids = set()
    neo4j_rel_ids = set()
    paths_obj = []
    for row_ in results:
        row = {
            'metapath': metapath.abbrev,
        }
        row.update(row_)
        row['score'] = None if metapath_score is None else metapath_score * row['percent_of_DWPC']
        neo4j_node_ids.update(row['node_ids'])
        neo4j_rel_ids.update(row['rel_ids'])
        paths_obj.append(row)

    node_id_to_info = get_neo4j_node_info(neo4j_node_ids)
    rel_id_to_info = get_neo4j_rel_info(neo4j_rel_ids)
    # TODO return better path_count_info when pathcount_record=None
    from dj_hetmech_app.serializers import PathCountDgpSerializer
    path_count_info = PathCountDgpSerializer(pathcount_record).data if pathcount_record else {}
    json_obj = {
        'query': {
            'source_id': source_id,
            'target_id': target_id,
            'source_metanode': source_record.metanode.identifier,
            'target_metanode': target_record.metanode.identifier,
            'source_identifier': source_identifier,
            'target_identifier': target_identifier,
            'metapath': metapath.abbrev,
            'metapath_id': [edge.get_id() for edge in metapath],
            'metapath_unadjusted_p_value': pathcount_record.p_value if pathcount_record else None,
            'metapath_adjusted_p_value': adj_p_value if pathcount_record else None,
            'metapath_score': metapath_score,
            'limit': limit,
        },
        # TODO: path_count_info will replace most fields in query in the future.
        'path_count_info': path_count_info,
        'paths': paths_obj,
        'nodes': node_id_to_info,
        'relationships': rel_id_to_info,
    }
    return json_obj


cypher_node_query = '''\
MATCH (node)
WHERE id(node) IN $node_ids
RETURN
  id(node) AS neo4j_id,
  head(labels(node)) AS node_label,
  properties(node) AS properties
ORDER BY neo4j_id
'''


def get_neo4j_node_info(node_ids):
    """
    Return information on nodes corresponding to the input neo4j node ids.
    """
    node_ids = sorted(node_ids)
    driver = get_neo4j_driver()
    with driver.session() as session:
        results = session.run(cypher_node_query, node_ids=node_ids)
        results = [dict(record) for record in results]
    metagraph = get_hetionet_metagraph()
    for record in results:
        metanode = metagraph.get_metanode(record['node_label'])
        record['metanode'] = metanode.identifier
    id_to_info = {x['neo4j_id']: x for x in results}
    return id_to_info


cypher_rel_query = '''\
MATCH ()-[rel]->()
WHERE id(rel) in $rel_ids
RETURN
  id(rel) AS neo4j_id,
  type(rel) AS rel_type,
  id(startNode(rel)) AS source_neo4j_id,
  id(endNode(rel)) AS target_neo4j_id,
  properties(rel) AS properties
ORDER BY neo4j_id
'''


def get_neo4j_rel_info(rel_ids):
    """
    Return information on relationships corresponding to the
    input neo4j relationship ids.
    """
    rel_ids = sorted(rel_ids)
    driver = get_neo4j_driver()
    with driver.session() as session:
        results = session.run(cypher_rel_query, rel_ids=rel_ids)
        results = [dict(record) for record in results]
    metagraph = get_hetionet_metagraph()
    for record in results:
        metaedge = metagraph.get_metaedge(record['rel_type'])
        record['kind'] = metaedge.kind
        record['directed'] = metaedge.direction != 'both'
    id_to_info = {x['neo4j_id']: x for x in results}
    return id_to_info


def get_metapath_counts_for_node(node, metanodes: list = None):
    """
    Return a dictionary (collections.Counter) of the number of metapaths from
    the input node to each other node in the PathCounts table.

    `metanodes` is a list of metanode abbreviations, like `['G', 'MF']`, to subset 
    other nodes by metanode. The default `metanodes=None` does not filter by metanode.
    """
    from django.db.models import Count, F
    from dj_hetmech_app.models import PathCount
    query_set = (
        PathCount.objects
        .annotate(search_against=F('source'), node=F('target'))
        .filter(search_against=node)
        .values('node')
        .annotate(n_metapaths=Count('node'))
        .filter(**({} if metanodes is None else {'target__metanode__abbreviation__in': metanodes}))
        .order_by('-n_metapaths')
    ).union((
        PathCount.objects
        .annotate(search_against=F('target'), node=F('source'))
        .filter(search_against=node)
        .values('node')
        .annotate(n_metapaths=Count('node'))
        .filter(**({} if metanodes is None else {'source__metanode__abbreviation__in': metanodes}))
        .order_by('-n_metapaths')
    ), all=True)

    counter = collections.Counter()
    for result in query_set:
        counter[result['node']] += result['n_metapaths']
    return counter


def get_metapath_queryset(source_metanode, target_metanode, extra_filters=None):
    """
    Find metapaths between a source and target metanode.
    Get back Metapath table records, with an added reversed field.

    WARNING: django cannot filter union querysets and does not create
    a warning (https://stackoverflow.com/a/49261966/4651668).
    """
    from dj_hetmech_app.models import Metapath
    from django.db.models import Value, BooleanField
    if extra_filters is None:
        from django.db.models import Q
        extra_filters = Q()
    metapath_qs = (
        Metapath.objects.filter(extra_filters, source=source_metanode, target=target_metanode)
        .annotate(reversed=Value(False, output_field=BooleanField()))
    )
    if source_metanode != target_metanode:
        # Do not use |= instead of .union since it interferes with reversed=True
        metapath_qs = metapath_qs.union(
            Metapath.objects.filter(extra_filters, source=target_metanode, target=source_metanode)
            .annotate(reversed=Value(True, output_field=BooleanField()))
        )
    return metapath_qs


def get_pathcount_queryset(source_node, target_node, extra_filters=None):
    """
    Find pathcount records between a source and target node.
    Get back Pathcount table records, with an added reversed field.
    """
    from dj_hetmech_app.models import PathCount
    from django.db.models import Value, BooleanField
    if extra_filters is None:
        from django.db.models import Q
        extra_filters = Q()
    pathcount_qs = (
        PathCount.objects.filter(extra_filters, source=source_node, target=target_node)
        .annotate(reversed=Value(False, output_field=BooleanField()))
    )
    if source_node != target_node:
        pathcount_qs = pathcount_qs.union(
            PathCount.objects.filter(extra_filters, source=target_node, target=source_node)
            .annotate(reversed=Value(True, output_field=BooleanField()))
        )
    return pathcount_qs


def get_metapath_instance(metapath):
    from dj_hetmech_app.models import Metapath
    from django.db.models import Value, BooleanField
    if isinstance(metapath, Metapath):
        return metapath
    if isinstance(metapath, str):
        from . import metapath_from_abbrev
        metapath = metapath_from_abbrev(metapath)
    abbrev_original = metapath.abbrev
    abbrev_reversed = metapath.inverse.abbrev
    metapath_qs = (
        Metapath.objects.filter(abbreviation=abbrev_original)
        .annotate(reversed=Value(False, output_field=BooleanField()))
    )
    if abbrev_original != abbrev_reversed:
        # Do not use |= instead of .union since it interferes with reversed=True
        metapath_qs = metapath_qs.union(
            Metapath.objects.filter(abbreviation=abbrev_reversed)
            .annotate(reversed=Value(True, output_field=BooleanField()))
        )
    return metapath_qs.first()
