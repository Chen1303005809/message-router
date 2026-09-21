from __future__ import annotations

from kefu.media.storage import InMemoryObjectStorage, MediaIngestor
from kefu.persistence.models import StoredMedia
from kefu.wecom.transport import InboundImagePart
from tests.conftest import DeskContext


def test_media_ingestor_persists_metadata_and_private_object(desk_context: DeskContext) -> None:
    storage = InMemoryObjectStorage()
    media_id = MediaIngestor(desk_context.session_factory, storage).ingest(
        InboundImagePart(data=b"image-payload", mime_type="image/png")
    )
    with desk_context.session_factory() as session:
        media = session.get(StoredMedia, media_id)
        assert media is not None
        assert media.object_key in storage.objects
        assert storage.objects[media.object_key][0] == b"image-payload"
