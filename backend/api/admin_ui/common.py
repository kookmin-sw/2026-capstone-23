from __future__ import annotations

import copy
import json
from typing import Any, MutableMapping, Optional, Sequence

from starlette.requests import Request
from starlette_admin import BaseModelView
from starlette_admin.exceptions import ActionFailed, FormValidationError
from starlette_admin.fields import JSONField, StringField

from core.version import PUBLIC_API_PREFIX

ADMIN_API_PREFIX = PUBLIC_API_PREFIX
FINAL_JOB_STATES = {"COMPLETED", "FAILED", "CANCELED"}


def clone_value(value: Any) -> Any:
    return copy.deepcopy(value)


def record_sort_value(value: Any) -> tuple[int, Any]:
    if value is None:
        return (1, "")
    if isinstance(value, (str, int, float, bool)):
        return (0, value)
    return (0, json.dumps(value, ensure_ascii=False, sort_keys=True))


def contains_term(value: Any, term: str) -> bool:
    if value is None:
        return False
    if isinstance(value, (dict, list)):
        return term in json.dumps(value, ensure_ascii=False, sort_keys=True).lower()
    return term in str(value).lower()


class StoreRecord:
    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def __getattr__(self, name: str) -> Any:
        return self._data.get(name)

    def as_dict(self) -> dict[str, Any]:
        return clone_value(self._data)


class BaseStoreAdminView(BaseModelView):
    identity: str = ""
    label: str = ""
    name: str = ""
    icon: Optional[str] = None
    pk_attr: str = "recordKey"
    fields: Sequence[Any] = []
    searchable_fields: Sequence[str] = ()
    sortable_fields: Sequence[str] = ()
    search_builder = False
    page_size = 25
    fields_default_sort = ("recordKey",)

    def __init__(
        self,
        *,
        store: MutableMapping[str, dict[str, Any]],
        allow_create: bool = False,
        allow_edit: bool = False,
        allow_delete: bool = False,
    ) -> None:
        self.store = store
        self.allow_create = allow_create
        self.allow_edit = allow_edit
        self.allow_delete = allow_delete
        super().__init__()

    def can_create(self, request: Request) -> bool:
        return self.allow_create

    def can_edit(self, request: Request) -> bool:
        return self.allow_edit

    def can_delete(self, request: Request) -> bool:
        return self.allow_delete

    def _get_raw(self, key: str) -> dict[str, Any]:
        raw = self.store.get(key)
        if raw is None:
            raise ActionFailed(f"Missing record: {key}")
        return clone_value(raw)

    def _serialize_record(self, key: str, raw: dict[str, Any]) -> StoreRecord:
        return StoreRecord(self.build_record_data(key, raw))

    def build_record_data(self, key: str, raw: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    def _filtered_records(self, where: dict[str, Any] | str | None) -> list[StoreRecord]:
        records = [self._serialize_record(key, clone_value(raw)) for key, raw in self.store.items()]
        if not where or not isinstance(where, str):
            return records
        term = where.strip().lower()
        if not term:
            return records

        results: list[StoreRecord] = []
        for record in records:
            data = record.as_dict()
            haystacks = [data.get(self.pk_attr)]
            for field_name in self.searchable_fields:
                haystacks.append(data.get(field_name))
            haystacks.append(data.get("payload"))
            if any(contains_term(value, term) for value in haystacks):
                results.append(record)
        return results

    def _sorted_records(self, records: list[StoreRecord], order_by: Optional[list[str]]) -> list[StoreRecord]:
        if not order_by:
            return records
        sorted_records = list(records)
        for clause in reversed(order_by):
            field_name, _, direction = clause.partition(" ")
            reverse = direction.strip().lower() == "desc"
            sorted_records.sort(key=lambda record: record_sort_value(getattr(record, field_name, None)), reverse=reverse)
        return sorted_records

    async def find_all(
        self,
        request: Request,
        skip: int = 0,
        limit: int = 100,
        where: dict[str, Any] | str | None = None,
        order_by: Optional[list[str]] = None,
    ) -> Sequence[Any]:
        records = self._sorted_records(self._filtered_records(where), order_by)
        return records[skip : skip + limit]

    async def count(self, request: Request, where: dict[str, Any] | str | None = None) -> int:
        return len(self._filtered_records(where))

    async def find_by_pk(self, request: Request, pk: Any) -> Any:
        key = str(pk)
        return self._serialize_record(key, self._get_raw(key))

    async def find_by_pks(self, request: Request, pks: list[Any]) -> Sequence[Any]:
        records: list[StoreRecord] = []
        for pk in pks:
            key = str(pk)
            raw = self.store.get(key)
            if raw is not None:
                records.append(self._serialize_record(key, clone_value(raw)))
        return records

    async def create(self, request: Request, data: dict[str, Any]) -> Any:
        if not self.can_create(request):
            raise ActionFailed("Create is disabled for this view.")
        key, raw = self.create_raw_record(request, data)
        self.store[key] = raw
        return self._serialize_record(key, clone_value(raw))

    async def edit(self, request: Request, pk: Any, data: dict[str, Any]) -> Any:
        if not self.can_edit(request):
            raise ActionFailed("Edit is disabled for this view.")
        key = str(pk)
        raw = self.edit_raw_record(request, key, data)
        self.store[key] = raw
        return self._serialize_record(key, clone_value(raw))

    async def delete(self, request: Request, pks: list[Any]) -> Optional[int]:
        if not self.can_delete(request):
            raise ActionFailed("Delete is disabled for this view.")
        removed = 0
        for pk in pks:
            key = str(pk)
            if self.store.pop(key, None) is not None:
                removed += 1
        return removed

    def create_raw_record(self, request: Request, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        raise ActionFailed("Create is not implemented for this view.")

    def edit_raw_record(self, request: Request, key: str, data: dict[str, Any]) -> dict[str, Any]:
        raise ActionFailed("Edit is not implemented for this view.")


class JsonStoreAdminView(BaseStoreAdminView):
    def __init__(
        self,
        *,
        store: MutableMapping[str, dict[str, Any]],
        identity: str,
        label: str,
        icon: str,
        summary_fields: Sequence[Any],
        searchable_fields: Sequence[str],
        sortable_fields: Sequence[str],
        default_sort: Sequence[tuple[str, bool] | str] | None = None,
        allow_create: bool = False,
        allow_edit: bool = False,
        allow_delete: bool = False,
    ) -> None:
        self.identity = identity
        self.label = label
        self.name = label.rstrip("s")
        self.icon = icon
        self.pk_attr = "recordKey"
        self.searchable_fields = tuple(searchable_fields)
        self.sortable_fields = tuple(sortable_fields)
        self.fields_default_sort = tuple(default_sort or ("recordKey",))
        self.fields = [
            StringField("recordKey", label="Key", read_only=True, required=True, exclude_from_edit=True),
            *summary_fields,
            JSONField("payload", label="Payload", read_only=not (allow_create or allow_edit), exclude_from_list=True, required=False),
        ]
        super().__init__(store=store, allow_create=allow_create, allow_edit=allow_edit, allow_delete=allow_delete)

    def build_record_data(self, key: str, raw: dict[str, Any]) -> dict[str, Any]:
        data = {"recordKey": key, "payload": clone_value(raw)}
        for field in self.fields:
            if field.name in {"recordKey", "payload"}:
                continue
            data[field.name] = raw.get(field.name)
        return data

    def create_raw_record(self, request: Request, data: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        key = str(data.get("recordKey") or "").strip()
        if not key:
            raise FormValidationError({"recordKey": "Key is required."})
        if key in self.store:
            raise FormValidationError({"recordKey": "Key already exists."})
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise FormValidationError({"payload": "Payload must be a JSON object."})
        return key, payload

    def edit_raw_record(self, request: Request, key: str, data: dict[str, Any]) -> dict[str, Any]:
        if key not in self.store:
            raise ActionFailed(f"Missing record: {key}")
        payload = data.get("payload")
        if not isinstance(payload, dict):
            raise FormValidationError({"payload": "Payload must be a JSON object."})
        return payload
