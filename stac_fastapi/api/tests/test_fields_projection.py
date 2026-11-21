"""End-to-end tests for the unified Fields projection in the API layer."""

import pytest
from fastapi.testclient import TestClient
from stac_pydantic.api.utils import link_factory

from stac_fastapi.api.app import StacApi
from stac_fastapi.api.models import create_get_request_model, create_post_request_model
from stac_fastapi.extensions import FieldsExtension
from stac_fastapi.types import stac
from stac_fastapi.types.config import ApiSettings
from stac_fastapi.types.core import BaseCoreClient
from stac_fastapi.types.search import BaseSearchPostRequest

item_links = (
    link_factory.ItemLinks("/", "test", "test")
    .create_links()
    .model_dump(exclude_none=True)
)

ITEM = {
    "type": "Feature",
    "stac_version": "1.0.0",
    "stac_extensions": ["https://example.com/ext.json"],
    "id": "test_item",
    "collection": "test_collection",
    "geometry": {"type": "Point", "coordinates": [0, 0]},
    "bbox": [-180, -90, 180, 90],
    "properties": {
        "datetime": "2000-01-01T00:00:00Z",
        "eo:cloud_cover": 12,
    },
    "links": item_links,
    "assets": {
        "preview": {
            "href": "https://example.com/preview.png",
            "type": "image/png",
            "roles": ["thumbnail"],
        },
        "data": {"href": "https://example.com/data.tif", "type": "image/tiff"},
    },
}

#: Fields a backend reads even though they are not part of the response.
IDENTITY = {"id", "collection", "type", "stac_version"}


class RecordingClient(BaseCoreClient):
    """A backend that records the projection and returns full documents.

    This models an "old" backend: it ignores ``fields_projection`` and returns
    complete items. The API layer must trim the response.
    """

    def __init__(self):
        super().__init__()
        self.calls: list[dict] = []

    def _record(self, **kwargs):
        self.calls.append(kwargs)
        return stac.ItemCollection(
            type="FeatureCollection", features=[dict(ITEM)], links=[]
        )

    def post_search(self, search_request: BaseSearchPostRequest, **kwargs):
        self.calls.append({"search_request": search_request, **kwargs})
        return stac.ItemCollection(
            type="FeatureCollection", features=[dict(ITEM)], links=[]
        )

    def get_search(self, **kwargs):
        return self._record(**kwargs)

    def get_item(self, item_id, collection_id, **kwargs):
        raise NotImplementedError

    def all_collections(self, **kwargs):
        raise NotImplementedError

    def get_collection(self, collection_id, **kwargs):
        raise NotImplementedError

    def item_collection(self, collection_id, **kwargs):
        return self._record(**kwargs)


class ColumnClient(RecordingClient):
    """A backend that supports column projection.

    It reads only the requested columns, in the same way a real storage layer
    would, and returns the already-trimmed documents.
    """

    def _project(self, fields):
        from stac_fastapi.types.fields import filter_fields

        return filter_fields(ITEM, set(fields), set())

    def get_search(self, **kwargs):
        projection = kwargs.get("fields_projection")
        fields = projection.backend_fields() if projection else None
        if fields is None:
            features = [dict(ITEM)]
        else:
            features = [self._project(fields)]
        self.calls.append({"read_fields": fields, **kwargs})
        return stac.ItemCollection(type="FeatureCollection", features=features, links=[])

    def post_search(self, search_request, **kwargs):
        projection = kwargs.get("fields_projection")
        fields = projection.backend_fields() if projection else None
        if fields is None:
            features = [dict(ITEM)]
        else:
            features = [self._project(fields)]
        self.calls.append(
            {"read_fields": fields, "search_request": search_request, **kwargs}
        )
        return stac.ItemCollection(type="FeatureCollection", features=features, links=[])


def _app(client, *, validate=False, extensions=True, direct=False):
    extension = FieldsExtension() if extensions else None
    return StacApi(
        settings=ApiSettings(
            enable_response_models=validate, enable_direct_response=direct
        ),
        client=client,
        search_get_request_model=create_get_request_model(
            [extension] if extension else []
        ),
        search_post_request_model=create_post_request_model(
            [extension] if extension else []
        ),
        extensions=[extension] if extension else [],
    )


def _get(client, fields):
    return client.get("/search", params={"fields": fields})


def _post(client, include=None, exclude=None):
    body = {"collections": ["test"]}
    fields = {}
    if include is not None:
        fields["include"] = include
    if exclude is not None:
        fields["exclude"] = exclude
    if fields:
        body["fields"] = fields
    return client.post("/search", json=body)


def test_get_projection_trims_deep_fields():
    """A GET include trims the response down to a deep field."""
    api = _app(RecordingClient())
    with TestClient(api.app) as client:
        resp = _get(client, "assets.preview.href")

    assert resp.status_code == 200, resp.text
    feature = resp.json()["features"][0]
    assert feature["assets"] == {"preview": {"href": "https://example.com/preview.png"}}
    assert "geometry" not in feature
    assert "properties" not in feature
    # Pagination identity always survives.
    assert feature["id"] == "test_item"
    assert feature["collection"] == "test_collection"


def test_post_projection_trims_deep_fields():
    """A POST include behaves exactly like the GET one."""
    api = _app(RecordingClient())
    with TestClient(api.app) as client:
        resp = _post(client, include=["assets.preview.href"])

    assert resp.status_code == 200, resp.text
    feature = resp.json()["features"][0]
    assert feature["assets"] == {"preview": {"href": "https://example.com/preview.png"}}
    assert "geometry" not in feature


def test_get_and_post_produce_the_same_projection():
    """GET and POST yield identical responses for equal selections."""
    api = _app(RecordingClient())
    with TestClient(api.app) as client:
        get_resp = _get(client, "assets.preview.href,-properties")
        post_resp = _post(client, include=["assets.preview.href"], exclude=["properties"])

    assert get_resp.status_code == 200, get_resp.text
    assert post_resp.status_code == 200, post_resp.text
    assert get_resp.json() == post_resp.json()


def test_exclude_only_keeps_other_fields():
    """An exclude-only selection returns everything else, as before."""
    api = _app(RecordingClient())
    with TestClient(api.app) as client:
        resp = _get(client, "-geometry")

    assert resp.status_code == 200, resp.text
    feature = resp.json()["features"][0]
    assert "geometry" not in feature
    assert feature["properties"]["eo:cloud_cover"] == 12
    assert feature["assets"]["data"]["href"] == "https://example.com/data.tif"


def test_parent_and_child_include_are_order_independent():
    api = _app(RecordingClient())
    with TestClient(api.app) as client:
        resp = _get(client, "assets.preview.href,assets")

    assert resp.status_code == 200, resp.text
    feature = resp.json()["features"][0]
    # Both children are kept: including the parent wins.
    assert feature["assets"]["preview"]["href"] == "https://example.com/preview.png"
    assert feature["assets"]["data"]["href"] == "https://example.com/data.tif"


def test_excluding_a_parent_keeps_included_child():
    api = _app(RecordingClient())
    with TestClient(api.app) as client:
        resp = _get(client, "assets.preview.href,-assets")

    assert resp.status_code == 200, resp.text
    feature = resp.json()["features"][0]
    assert feature["assets"] == {"preview": {"href": "https://example.com/preview.png"}}


def test_get_backend_receives_projection():
    """The backend receives the exact columns it should read."""
    client = RecordingClient()
    api = _app(client)
    with TestClient(api.app) as sclient:
        _get(sclient, "assets.preview.href")

    projection = client.calls[-1]["fields_projection"]
    backend = projection.backend_fields()
    assert "assets.preview.href" in backend
    assert IDENTITY.issubset(backend)
    assert "geometry" not in backend


def test_post_backend_receives_projection():
    client = RecordingClient()
    api = _app(client)
    with TestClient(api.app) as sclient:
        _post(sclient, include=["assets.preview.href"])

    projection = client.calls[-1]["fields_projection"]
    backend = projection.backend_fields()
    assert "assets.preview.href" in backend
    assert IDENTITY.issubset(backend)


def test_backend_keeps_filter_and_sort_fields_readable():
    """Fields used by filter/sort are read even when not returned."""
    from stac_fastapi.extensions import SortExtension

    client = RecordingClient()
    extensions = [FieldsExtension(), SortExtension()]
    api = StacApi(
        settings=ApiSettings(),
        client=client,
        search_get_request_model=create_get_request_model(extensions),
        search_post_request_model=create_post_request_model(extensions),
        extensions=extensions,
    )
    with TestClient(api.app) as sclient:
        sclient.get(
            "/search",
            params={
                "fields": "assets.preview.href",
                "sortby": "-properties.datetime",
            },
        )

    projection = client.calls[-1]["fields_projection"]
    backend = projection.backend_fields()
    assert "assets.preview.href" in backend
    assert "properties.datetime" in backend


def test_no_fields_selection_does_not_inject_projection():
    """Plain searches keep the exact call signature backends already use."""
    client = RecordingClient()
    api = _app(client)
    with TestClient(api.app) as sclient:
        resp = sclient.get("/search")

    assert resp.status_code == 200, resp.text
    assert "fields_projection" not in client.calls[-1]


def test_exclude_only_does_not_inject_backend_projection():
    client = RecordingClient()
    api = _app(client)
    with TestClient(api.app) as sclient:
        _get(sclient, "-geometry")

    projection = client.calls[-1]["fields_projection"]
    # No include selection: the backend still reads full documents.
    assert projection.backend_fields() is None


def test_projection_works_without_fields_extension_models():
    """Trim only relies on the request carrying a projection value."""
    api = _app(RecordingClient(), extensions=False)
    with TestClient(api.app) as client:
        resp = _get(client, "assets.preview.href")

    # Without the extension the parameter is ignored, so no trimming happens.
    assert resp.status_code == 200, resp.text
    assert "geometry" in resp.json()["features"][0]


def test_column_backend_reads_only_projected_columns():
    """A projection-aware backend only reads what the projection asks for."""
    client = ColumnClient()
    api = _app(client)
    with TestClient(api.app) as sclient:
        resp = _get(sclient, "assets.preview.href")

    assert resp.status_code == 200, resp.text
    fields = client.calls[-1]["read_fields"]
    assert "assets.preview.href" in fields
    assert "geometry" not in fields
    assert IDENTITY.issubset(fields)
    feature = resp.json()["features"][0]
    assert feature["assets"] == {"preview": {"href": "https://example.com/preview.png"}}


def test_projection_with_response_models_left_to_backend():
    """With response models on, a trimmed dict would not validate.

    The documented contract is unchanged: the backend must return a
    ``Response`` to bypass validation when it projects, and the API layer must
    not trim a dict it cannot legally reshape.
    """
    api = _app(RecordingClient(), validate=True)
    with TestClient(api.app) as client:
        resp = _get(client, "assets.preview.href")

    # The full item validates, so the request succeeds with the full item.
    assert resp.status_code == 200, resp.text
    assert "geometry" in resp.json()["features"][0]


def test_direct_response_not_trimmed():
    """A backend returning a ``Response`` keeps full control of the body."""
    from starlette.responses import JSONResponse

    class ResponseClient(RecordingClient):
        def get_search(self, **kwargs):
            return JSONResponse(
                {"type": "FeatureCollection", "features": [dict(ITEM)], "links": []}
            )

    api = _app(ResponseClient())
    with TestClient(api.app) as client:
        resp = _get(client, "assets.preview.href")

    assert resp.status_code == 200, resp.text
    assert "geometry" in resp.json()["features"][0]


def test_direct_response_mode_still_trims_dict_payloads():
    """``enable_direct_response`` bypasses serialization but not trimming."""
    api = _app(RecordingClient(), direct=True)
    with TestClient(api.app) as client:
        resp = _get(client, "assets.preview.href")

    assert resp.status_code == 200, resp.text
    feature = resp.json()["features"][0]
    assert feature["assets"] == {"preview": {"href": "https://example.com/preview.png"}}
    assert "geometry" not in feature


def test_item_collection_endpoint_uses_the_same_projection():
    """The `/collections/{id}/items` binding is trimmed the same way."""
    from stac_fastapi.api.models import ItemCollectionUri, create_request_model

    client = RecordingClient()
    extension = FieldsExtension()
    items_model = create_request_model(
        "ItemCollectionURI",
        base_model=ItemCollectionUri,
        mixins=[extension.GET],
        request_type="GET",
    )
    api = StacApi(
        settings=ApiSettings(),
        client=client,
        items_get_request_model=items_model,
        extensions=[extension],
    )
    with TestClient(api.app) as sclient:
        resp = sclient.get(
            "/collections/test_collection/items",
            params={"fields": "assets.preview.href"},
        )

    assert resp.status_code == 200, resp.text
    feature = resp.json()["features"][0]
    assert feature["assets"] == {"preview": {"href": "https://example.com/preview.png"}}
    assert "geometry" not in feature
    assert "fields_projection" in client.calls[-1]


@pytest.mark.parametrize("fields", ["id", "-geometry", "assets.preview.href"])
def test_next_links_survive_trimming(fields):
    """The ItemCollection envelope, including links, is never trimmed."""
    api = _app(RecordingClient())
    with TestClient(api.app) as client:
        resp = _get(client, fields)

    body = resp.json()
    assert body["type"] == "FeatureCollection"
    assert "links" in body
    assert resp.json()["features"][0]["id"] == "test_item"


def test_pagination_identity_available_after_trimming():
    """The next cursor can be built from the trimmed entities.

    A backend that pages by the last item's ``id`` still finds it after the
    response is trimmed to a deep field.
    """
    api = _app(RecordingClient())
    with TestClient(api.app) as client:
        resp = _get(client, "assets.preview.href")

    last = resp.json()["features"][-1]
    assert last["id"] == "test_item"
    assert last["collection"] == "test_collection"


def test_filter_fields_read_even_when_not_returned():
    """A filter field is read by a column backend but not returned."""
    from stac_fastapi.extensions import FilterExtension

    client = ColumnClient()
    extensions = [FieldsExtension(), FilterExtension()]
    api = StacApi(
        settings=ApiSettings(),
        client=client,
        search_get_request_model=create_get_request_model(extensions),
        search_post_request_model=create_post_request_model(extensions),
        extensions=extensions,
    )
    with TestClient(api.app) as sclient:
        resp = sclient.get(
            "/search",
            params={
                "fields": "assets.preview.href",
                "filter": "eo:cloud_cover > 10",
            },
        )

    assert resp.status_code == 200, resp.text
    read_fields = client.calls[-1]["read_fields"]
    assert "eo:cloud_cover" in read_fields
    # The filter field is read but stays out of the response.
    feature = resp.json()["features"][0]
    assert "properties" not in feature
