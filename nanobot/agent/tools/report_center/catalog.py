"""Tenant/model catalog reconciliation against the live Cube catalog."""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from loguru import logger

from nanobot.agent.tools.report_center.phrases import (
    _TenantMentionResolution,
)


class _CatalogReconciliationMixin:

    async def _load_catalog_tenant_mentions(
        self, text: str
    ) -> tuple[list[dict[str, Any]], bool]:
        """Load only live catalog records that can be used to verify text mentions.

        The direct subscription path must fail closed when the catalog cannot
        be read.  A classifier result is not an identity proof, so retaining it
        after a failed scan would recreate the original one-customer-loss bug
        and could also turn an arbitrary phrase into a tenant selector.
        """

        finder = getattr(self._magik_tool, "find_tenant_mentions", None)
        loader = getattr(self._magik_tool, "list_tenant_catalog", None)
        if callable(finder):
            try:
                found = await finder(text, limit=20)
                if isinstance(found, list):
                    return [item for item in found if isinstance(item, dict)], True
            except Exception as exc:
                logger.warning(
                    "Cube subscription tenant mention scan failed: error_type={}",
                    type(exc).__name__,
                )
            return [], False
        if callable(loader):
            try:
                loaded = await loader(limit=20)
                if isinstance(loaded, list):
                    return [item for item in loaded if isinstance(item, dict)], True
            except Exception as exc:
                logger.warning(
                    "Cube subscription tenant mention scan failed: error_type={}",
                    type(exc).__name__,
                )
        return [], False


    async def _merge_catalog_tenant_mentions(
        self,
        text: str,
        aliases: tuple[str, ...],
        *,
        require_catalog: bool = False,
    ) -> tuple[str, ...] | None:
        """Reconcile classifier names with labels verified by the live catalog.

        ``require_catalog`` is used by natural-language subscription routing.
        It returns ``None`` when verification is unavailable, allowing the
        caller to present a recovery flow instead of silently executing a
        narrowed scope.  The default keeps the helper's historical tuple
        contract for compatibility adapters.
        """

        if require_catalog:
            # Keep the historical helper name for adapters, but route strict
            # callers through the same ambiguity/unresolved handling used by
            # natural-language subscriptions. Two strict implementations would
            # eventually reintroduce silent scope narrowing.
            resolution = await self._resolve_catalog_tenant_mentions(text, aliases)
            return None if resolution.error else resolution.values

        catalog, catalog_available = await self._load_catalog_tenant_mentions(text)

        classifier_values: list[str] = []
        for alias in aliases:
            classifier_values.extend(
                part.strip()
                for part in re.split(r"[,，、;；\n]+", str(alias))
                if part.strip()
            )

        configured = getattr(self._cube_config, "tenant_mappings", {}) or {}
        candidates: list[tuple[str, str]] = []
        for item in catalog:
            tenant_id = str(item.get("tenant_id") or item.get("tenantId") or "").strip()
            if not tenant_id:
                continue
            labels = [
                str(item.get("matched_label") or "").strip(),
                str(item.get("display_name") or item.get("displayName") or "").strip(),
                str(item.get("name") or "").strip(),
                tenant_id,
            ]
            labels.extend(
                str(alias).strip()
                for alias, target in configured.items()
                if str(target).strip() == tenant_id
            )
            for label in dict.fromkeys(label for label in labels if label):
                # Generic catalog tags such as “客户” can occur in the
                # surrounding sentence and are not safe tenant identities.
                if label.casefold() in {"客户", "租户", "用户", "customer", "tenant"}:
                    continue
                candidates.append((label, tenant_id))

        # Find catalog labels in their original order and choose the longest
        # non-overlapping label.  This prevents a short alias from masking a
        # longer customer name, while ensuring two explicitly named customers
        # are both retained when the LLM returned only one of them.
        folded = text.casefold()
        configured_aliases = {
            str(alias).strip().casefold(): str(target).strip()
            for alias, target in configured.items()
            if str(alias).strip() and str(target).strip()
        }
        occurrences: list[tuple[int, int, int, str, str]] = []
        for label, tenant_id in candidates:
            folded_label = label.casefold()
            start = folded.find(folded_label)
            if start >= 0:
                # A configured alias is the deterministic tie-breaker when
                # duplicate catalog records share the same display name. The
                # alias still has to point at a record returned by Cube.
                alias_priority = (
                    0 if configured_aliases.get(folded_label) == tenant_id else 1
                )
                occurrences.append((start, alias_priority, -len(label), label, tenant_id))
        selected_ids: set[str] = set()
        occupied: list[tuple[int, int]] = []
        # The compatibility mode historically returned classifier values even
        # when a test adapter did not expose a catalog.  Keep that behavior for
        # callers that explicitly opt out of strict verification; the direct
        # NLU path always passes ``require_catalog=True`` and starts empty.
        values: list[str] = list(dict.fromkeys(classifier_values)) if not require_catalog else []
        for start, _alias_priority, _negative_length, label, tenant_id in sorted(occurrences):
            end = start + len(label)
            if tenant_id in selected_ids or any(
                start < occupied_end and end > occupied_start
                for occupied_start, occupied_end in occupied
            ):
                continue
            selected_ids.add(tenant_id)
            occupied.append((start, end))
            values.append(label)
        return tuple(values)


    async def _resolve_catalog_tenant_mentions(
        self,
        text: str,
        aliases: tuple[str, ...],
    ) -> _TenantMentionResolution:
        """Resolve every named tenant against live IDs without narrowing scope.

        The classifier output is a hint rather than an identity proof.  The
        original sentence is scanned against live catalog labels so a model
        that returns only the last customer cannot drop earlier customers.
        Shared display names and tags fail closed unless a configured alias or
        exact tenant ID identifies one live record.
        """

        catalog, catalog_available = await self._load_catalog_tenant_mentions(text)
        if not catalog_available:
            return _TenantMentionResolution((), error="catalog_unavailable")

        classifier_values: list[str] = []
        for alias in aliases:
            classifier_values.extend(
                part.strip()
                for part in re.split(r"[,，、;；\n]+", str(alias))
                if part.strip()
            )
        classifier_values = list(dict.fromkeys(classifier_values))

        configured = getattr(self._cube_config, "tenant_mappings", {}) or {}
        configured_aliases = {
            str(alias).strip().casefold(): str(target).strip()
            for alias, target in configured.items()
            if str(alias).strip() and str(target).strip()
        }
        label_targets: dict[str, set[str]] = {}
        display_by_id: dict[str, str] = {}
        generic_labels = {"客户", "租户", "用户", "customer", "tenant"}
        for item in catalog:
            tenant_id = str(item.get("tenant_id") or item.get("tenantId") or "").strip()
            if not tenant_id:
                continue
            display = str(
                item.get("display_name")
                or item.get("displayName")
                or item.get("name")
                or tenant_id
            ).strip()
            display_by_id.setdefault(tenant_id, display or tenant_id)
            labels = [
                str(item.get("matched_label") or "").strip(),
                str(item.get("display_name") or item.get("displayName") or "").strip(),
                str(item.get("name") or "").strip(),
                tenant_id,
            ]
            labels.extend(
                str(alias).strip()
                for alias, target in configured.items()
                if str(target).strip() == tenant_id
            )
            for label in dict.fromkeys(label for label in labels if label):
                if label.casefold() not in generic_labels:
                    label_targets.setdefault(label.casefold(), set()).add(tenant_id)

        # Validate every classifier value.  A hallucinated or stale value must
        # be reported instead of being silently discarded when other names do
        # happen to match the sentence.  Some providers, however, serialize a
        # long Chinese list together with the report suffix (for example
        # ``阳春面、豆汁、佛跳墙全部模型日报简报``).  Treating that whole string
        # as one unknown alias would reproduce the original one-customer-loss
        # bug, so embedded live labels are reconciled before failing closed.
        classifier_targets: dict[str, set[str]] = {}
        unresolved: list[str] = []
        for value in classifier_values:
            folded_value = value.casefold()
            targets = set(label_targets.get(folded_value, set()))
            configured_target = configured_aliases.get(folded_value)
            if configured_target in targets:
                targets = {configured_target}
            if not targets:
                embedded: list[tuple[int, int, str, set[str]]] = []
                for label, label_targets_for_value in label_targets.items():
                    start = folded_value.find(label)
                    if start >= 0:
                        embedded.append(
                            (start, -len(label), label, set(label_targets_for_value))
                        )
                embedded.sort(key=lambda item: (item[0], item[1], item[2]))
                occupied: list[tuple[int, int]] = []
                embedded_targets: list[tuple[str, set[str]]] = []
                for start, _negative_length, label, label_target_set in embedded:
                    end = start + len(label)
                    if any(
                        start < occupied_end and end > occupied_start
                        for occupied_start, occupied_end in occupied
                    ):
                        continue
                    occupied.append((start, end))
                    embedded_targets.append((label, label_target_set))
                ambiguous = next(
                    (
                        label
                        for label, label_target_set in embedded_targets
                        if len(label_target_set) > 1
                    ),
                    None,
                )
                if ambiguous is not None:
                    return _TenantMentionResolution(
                        (), error="tenant_ambiguous", unresolved=(ambiguous,)
                    )
                if embedded_targets:
                    # Strip only known report/list glue from the remainder.
                    # Any other residual word remains unresolved, preserving
                    # fail-closed behavior for a value that mixes a live name
                    # with a hallucinated customer.
                    remainder = folded_value
                    for label, _label_target_set in embedded_targets:
                        remainder = remainder.replace(label, " ", 1)
                    remainder = re.sub(
                        r"(?:全部模型|所有模型|全模型|多客户|多模型|日报简报|日报|周报简报|周报|月报简报|月报|区间报表|报表|简报|客户|租户|模型|以及|并且|发送|推送|给我|每天|工作日|每周|每月|的|和|与|及|[\s,，、;；:：()（）\[\]【】])",
                        "",
                        remainder,
                        flags=re.IGNORECASE,
                    )
                    if remainder.strip():
                        unresolved.append(value)
                    else:
                        for label, label_target_set in embedded_targets:
                            classifier_targets[label] = set(label_target_set)
                else:
                    unresolved.append(value)
            elif len(targets) > 1:
                return _TenantMentionResolution(
                    (), error="tenant_ambiguous", unresolved=(value,)
                )
            else:
                classifier_targets[folded_value] = targets

        folded_text = text.casefold()
        occurrences: list[tuple[int, int, int, str, str]] = []
        for folded_label, raw_targets in label_targets.items():
            targets = set(raw_targets)
            preferred = configured_aliases.get(folded_label)
            if preferred in targets:
                targets = {preferred}
            classifier_hint = classifier_targets.get(folded_label)
            if classifier_hint and len(classifier_hint) == 1:
                targets &= classifier_hint
            if len(targets) > 1:
                if folded_label in folded_text:
                    return _TenantMentionResolution(
                        (), error="tenant_ambiguous", unresolved=(folded_label,)
                    )
                continue
            if not targets:
                continue
            tenant_id = next(iter(targets))
            start = folded_text.find(folded_label)
            while start >= 0:
                priority = 0 if preferred == tenant_id or classifier_hint else 1
                occurrences.append(
                    (start, priority, -len(folded_label), folded_label, tenant_id)
                )
                start = folded_text.find(
                    folded_label, start + max(1, len(folded_label))
                )

        selected_ids: set[str] = set()
        occupied: list[tuple[int, int]] = []
        selected: list[tuple[int, str]] = []
        for start, _priority, _negative_length, folded_label, tenant_id in sorted(occurrences):
            end = start + len(folded_label)
            if tenant_id in selected_ids or any(
                start < occupied_end and end > occupied_start
                for occupied_start, occupied_end in occupied
            ):
                continue
            selected_ids.add(tenant_id)
            occupied.append((start, end))
            selected.append((start, tenant_id))

        if unresolved:
            return _TenantMentionResolution(
                (), error="scope_unresolved", unresolved=tuple(unresolved)
            )
        # A valid classifier hint may be a real ID or a normalized label that
        # is not found byte-for-byte in the sentence.  It is safe to retain
        # because it already mapped to a live catalog record.
        for targets in classifier_targets.values():
            selected_ids.update(targets)
        if not selected_ids:
            return _TenantMentionResolution((), error="scope_unresolved")

        ordered_ids: list[str] = []
        for _start, tenant_id in sorted(selected):
            if tenant_id not in ordered_ids:
                ordered_ids.append(tenant_id)
        for tenant_id in sorted(selected_ids - set(ordered_ids)):
            ordered_ids.append(tenant_id)
        values = tuple(
            next(
                (
                    alias
                    for alias, target in configured.items()
                    if str(target).strip() == tenant_id
                ),
                display_by_id.get(tenant_id, tenant_id),
            )
            for tenant_id in ordered_ids
        )
        return _TenantMentionResolution(values)


    def _canonical_cube_models(self, values: tuple[str, ...]) -> tuple[str, ...]:
        """Resolve only configured shorthand; API model names otherwise pass through unchanged."""

        aliases = getattr(self._cube_config, "model_aliases", {}) or {}
        canonical: list[str] = []
        for value in values:
            normalized = value.strip()
            if not normalized:
                continue
            resolved = next(
                (
                    str(target).strip()
                    for alias, target in aliases.items()
                    if str(alias).strip().casefold() == normalized.casefold()
                ),
                normalized,
            )
            if resolved and resolved not in canonical:
                canonical.append(resolved)
        return tuple(canonical)


    async def _load_tenant_model_catalog(
        self, tenant_ids: list[str], *, start_date: date, end_date: date
    ) -> dict[str, list[str]]:
        """Load explicit model names for all-model multi-tenant execution.

        The Cube usage endpoint may collapse an omitted model into a tenant
        aggregate, so every manual and scheduled run expands the live catalog
        before entering ReportRunner.
        """

        if self._magik_tool is None:
            raise LookupError("Cube 模型目录当前不可用")

        # Cron and other non-interactive callers cannot consume the legacy
        # selector's ``agent_ui`` response.  The concrete Cube tool exposes a
        # direct catalog method for this path; keep the old selector fallback
        # only for compatibility adapters and test doubles that predate it.
        direct_loader = getattr(self._magik_tool, "list_active_models_for_tenants", None)
        native_loader = getattr(type(self._magik_tool), "list_active_models_for_tenants", None)
        if callable(direct_loader) and native_loader is not None:
            try:
                loaded = await direct_loader(
                    tenant_ids,
                    start_date=start_date,
                    end_date=end_date,
                )
            except Exception as exc:
                raise LookupError("Cube 实时模型目录未返回可用模型，请检查目录查询权限") from exc
            if not isinstance(loaded, dict):
                raise LookupError("Cube 实时模型目录返回格式无效")
            catalog_models = {
                str(tenant_id).strip(): list(
                    dict.fromkeys(
                        str(model).strip()
                        for model in models
                        if str(model).strip()
                    )
                )
                for tenant_id, models in loaded.items()
                if str(tenant_id).strip() and isinstance(models, (list, tuple))
            }
            missing = [tenant_id for tenant_id in tenant_ids if not catalog_models.get(tenant_id)]
            if missing:
                raise LookupError("Cube 实时模型目录未返回可用模型，请检查客户模型配置")
            if sum(len(catalog_models[tenant_id]) for tenant_id in tenant_ids) > 200:
                raise ValueError("客户模型组合超过 200 个，请缩小订阅范围")
            return {tenant_id: catalog_models[tenant_id] for tenant_id in tenant_ids}

        # Compatibility fallback for older adapters and test doubles that only
        # expose configured model catalogs. The concrete Cube tool above must
        # use active usage discovery so all-model reports do not include idle
        # configured models.
        direct_loader = getattr(self._magik_tool, "list_models_for_tenants", None)
        native_loader = getattr(type(self._magik_tool), "list_models_for_tenants", None)
        if callable(direct_loader) and native_loader is not None:
            try:
                loaded = await direct_loader(tenant_ids)
            except Exception as exc:
                raise LookupError("Cube 实时模型目录未返回可用模型，请检查目录查询权限") from exc
            if not isinstance(loaded, dict):
                raise LookupError("Cube 实时模型目录返回格式无效")
            catalog_models = {
                str(tenant_id).strip(): list(
                    dict.fromkeys(
                        str(model).strip()
                        for model in models
                        if str(model).strip()
                    )
                )
                for tenant_id, models in loaded.items()
                if str(tenant_id).strip() and isinstance(models, (list, tuple))
            }
            missing = [tenant_id for tenant_id in tenant_ids if not catalog_models.get(tenant_id)]
            if missing:
                raise LookupError("Cube 实时模型目录未返回可用模型，请检查客户模型配置")
            if sum(len(catalog_models[tenant_id]) for tenant_id in tenant_ids) > 200:
                raise ValueError("客户模型组合超过 200 个，请缩小订阅范围")
            return {tenant_id: catalog_models[tenant_id] for tenant_id in tenant_ids}

        catalog_result = await self._magik_tool.execute(
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            comparison="none",
            include_tpm=False,
            report_template="matrix_card",
            granularity="day",
            interactive=True,
            report_selections=[
                {
                    "tenant_query": tenant_id,
                    "model_scope": "selected",
                    "models": [],
                }
                for tenant_id in tenant_ids
            ],
            save_snapshot=False,
            _trusted_selection_limit=20,
        )
        catalog_ui = self._agent_ui_of(catalog_result)
        catalog_entries = (
            catalog_ui.get("tenant_models", [])
            if isinstance(catalog_ui, dict) and catalog_ui.get("phase") == "models"
            else []
        )
        catalog_models = {
            str(item.get("tenant_query") or "").strip(): [
                str(model_name).strip()
                for model_name in item.get("models") or []
                if str(model_name).strip()
            ]
            for item in catalog_entries
            if isinstance(item, dict) and str(item.get("tenant_query") or "").strip()
        }
        missing = [tenant_id for tenant_id in tenant_ids if not catalog_models.get(tenant_id)]
        if getattr(catalog_result, "is_error", False) or missing:
            raise LookupError(
                "Cube 实时模型目录未返回可用模型，请检查客户模型配置或目录查询权限"
            )
        if sum(len(catalog_models[tenant_id]) for tenant_id in tenant_ids) > 200:
            raise ValueError("客户模型组合超过 200 个，请缩小订阅范围")
        return {tenant_id: catalog_models[tenant_id] for tenant_id in tenant_ids}
