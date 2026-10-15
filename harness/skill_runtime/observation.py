"""Direct observations use the existing Worker lease, schema and redaction path."""
import base64
import hashlib

from harness._vendor.tau_agent.messages import ImageContent
from harness.agents.browser.review.task import READ_BROWSER_METHODS
from harness.skill_builder.session import _result


async def observe(session, *, method, params):
    state = session.store.load()
    page = state.get('browserBinding', {}).get('pageId')
    fleet = state.get('browserBinding', {}).get('fleetId')
    if method not in READ_BROWSER_METHODS:
        raise PermissionError('Only page observations are available through this tool')
    if not page or params.get('pageId', page) != page or params.get('fleetId', fleet) != fleet:
        raise PermissionError('Observation must use the current evidenced page binding')
    value = await session._delegate({'kind': 'observe', 'pageId': page, 'method': method,
        'params': {**params, 'pageId': page}}, 'Read the bound page; return facts without a Browser model loop')
    result = _result(value)
    fact = value.get('answer') or {}
    if fact.get('imageResourceUri'):
        row = session.logger.storage.read_resource(current_task_id=session.logger.task_id,
            resource_uri=fact['imageResourceUri'])
        pixels = row.get('content_blob') if row else None
        if isinstance(pixels, (bytes, memoryview)) and hashlib.sha256(pixels).hexdigest() == fact.get('imageSha256'):
            result.content.append(ImageContent(data=base64.b64encode(pixels).decode(), mime_type=row['media_type']))
    return result
