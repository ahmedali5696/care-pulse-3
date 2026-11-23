"""B-12 signed dated queue read surface."""

from fastapi import APIRouter, Request
from pydantic import ValidationError

from carepulse.identity.authorization import protected_transaction
from carepulse.identity.contracts import ContractError, success_response
from carepulse.queue.contracts import QueueQuery
from carepulse.queue.queries import dated_queue_projection

router = APIRouter(prefix="/v1/queue")


def _transaction(request: Request, *, stage: str = "PRIMARY"):
    identity = request.scope.get("carepulse.identity")
    if identity is None or identity.purpose != "QUEUE_READ":
        raise ContractError(403)
    return protected_transaction(
        request.app.state.db_engine,
        identity,
        request.app.state.service_config.policy.session_idle_minutes,
        stage=stage,
    )


@router.get("")
def get_queue(request: Request):
    params = request.query_params
    if len(params.multi_items()) != len(params):
        raise ContractError(422)
    try:
        query = QueueQuery.model_validate(dict(params))
    except ValidationError as invalid:
        raise ContractError(422) from invalid
    with _transaction(request) as (connection, actor):
        projection, revision = dated_queue_projection(connection, actor, query)
        return success_response(projection, revision)
