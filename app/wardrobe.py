import asyncio
import base64
import io
from datetime import datetime
from typing import Any, get_args
from uuid import UUID, uuid4

import httpx
from PIL import Image, ImageOps

from app.models import OutfitSlot, OutfitSuggestion, WardrobeItem
from app.tryon import MAX_OUTFIT_PIECES, OutfitPiece, TryOnError, TryOnService


SLOTS: tuple[str, ...] = get_args(OutfitSlot)
SLOT_LABELS = {
    "top": "top", "bottom": "bottom wear", "dress": "dress or one-piece", "outerwear": "jacket or layer",
    "footwear": "footwear", "jewelry": "jewelry", "accessory": "accessory", "other": "wearable item",
}
MAX_WARDROBE_ITEMS = 300
MAX_SUGGESTION_ITEMS = 40
SUGGESTION_THUMBNAIL = 384

SUGGESTION_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "outfits": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "title": {"type": "STRING"},
                    "reason": {"type": "STRING"},
                    "item_ids": {"type": "ARRAY", "items": {"type": "STRING"}},
                },
                "required": ["title", "reason", "item_ids"],
            },
        },
    },
    "required": ["outfits"],
}


def _thumbnail(image: tuple[bytes, str, str]) -> dict[str, Any]:
    with Image.open(io.BytesIO(image[0])) as picture:
        picture = ImageOps.exif_transpose(picture)
        picture.thumbnail((SUGGESTION_THUMBNAIL, SUGGESTION_THUMBNAIL))
        if picture.mode not in ("RGB", "L"):
            picture = picture.convert("RGB")
        buffer = io.BytesIO()
        picture.save(buffer, format="JPEG", quality=80)
    return {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(buffer.getvalue()).decode()}}


def _describe(row: dict[str, Any]) -> str:
    details = [SLOT_LABELS.get(row["slot"], row["slot"]), row["name"]]
    details += [value for value in (row.get("color"), row.get("brand")) if value]
    if row["collection"] == "store" and row.get("price") is not None:
        details.append(f"{row.get('currency') or 'INR'} {row['price']:g} at {row.get('store') or 'store'}")
    details.append("owned" if row["collection"] == "home" else "not yet bought")
    return " · ".join(str(value) for value in details)


class WardrobeService:
    """Saved shop products and the user's own clothes, stored in Supabase next to the try-on gallery."""

    def __init__(self, storage: TryOnService) -> None:
        self.storage = storage

    async def add(
        self,
        user_id: str,
        collection: str,
        slot: str,
        name: str,
        image: tuple[bytes, str, str],
        *,
        brand: str | None = None,
        color: str | None = None,
        price: float | None = None,
        currency: str | None = None,
        sizes: list[str] | None = None,
        selected_size: str | None = None,
        store: str | None = None,
        product_url: str | None = None,
        source_image_url: str | None = None,
        notes: str | None = None,
    ) -> WardrobeItem:
        if slot not in SLOTS:
            raise TryOnError(f"Category must be one of: {', '.join(SLOTS)}", 400)
        count = await self.storage.rest("HEAD", "wardrobe_items", params={"anonymous_user_id": f"eq.{user_id}", "select": "id"}, prefer="count=exact")
        total = count.headers.get("content-range", "").rpartition("/")[2]
        if count.status_code >= 400:
            raise TryOnError(self._table_error(count), 503)
        if total.isdigit() and int(total) >= MAX_WARDROBE_ITEMS:
            raise TryOnError(f"Your wardrobe is full ({MAX_WARDROBE_ITEMS} items). Remove some items first.", 409)
        item_id = str(uuid4())
        image_path = f"{user_id}/wardrobe/{item_id}.{image[2]}"
        await self.storage.upload(image_path, image)
        row = {
            "id": item_id, "anonymous_user_id": user_id, "collection": collection, "slot": slot,
            "name": name.strip()[:200] or SLOT_LABELS[slot].capitalize(), "brand": brand, "color": color, "price": price,
            "currency": currency, "sizes": (sizes or [])[:30], "selected_size": selected_size, "store": store,
            "product_url": product_url, "source_image_url": source_image_url, "notes": notes, "image_path": image_path,
        }
        response = await self.storage.rest("POST", "wardrobe_items", json_body=row, prefer="return=representation")
        if response.status_code >= 400:
            await self.storage.delete_objects([image_path])
            raise TryOnError(self._table_error(response), 503)
        return (await self._to_items(response.json()))[0]

    async def list_items(self, user_id: str, collection: str | None = None) -> list[WardrobeItem]:
        return await self._to_items(await self._rows(user_id, collection))

    async def delete(self, user_id: str, item_id: str) -> None:
        rows = await self._rows(user_id, ids=[item_id])
        if not rows:
            raise TryOnError("Wardrobe item not found", 404)
        response = await self.storage.rest("DELETE", "wardrobe_items", params={"id": f"eq.{item_id}", "anonymous_user_id": f"eq.{user_id}"})
        if response.status_code >= 400:
            raise TryOnError("Could not remove the wardrobe item")
        await self.storage.delete_objects([rows[0]["image_path"]])

    async def outfit_pieces(self, user_id: str, item_ids: list[str]) -> tuple[list[OutfitPiece], list[dict[str, Any]]]:
        """Load the user's chosen items, in a stable head-to-toe order, ready for the try-on model."""
        unique = list(dict.fromkeys(item_ids))
        if not 1 <= len(unique) <= MAX_OUTFIT_PIECES:
            raise TryOnError(f"Choose between 1 and {MAX_OUTFIT_PIECES} wardrobe items", 400)
        rows = await self._rows(user_id, ids=unique)
        if len(rows) != len(unique):
            raise TryOnError("Some selected wardrobe items no longer exist", 404)
        rows.sort(key=lambda row: SLOTS.index(row["slot"]) if row["slot"] in SLOTS else len(SLOTS))
        images = await asyncio.gather(*(self.storage.download(row["image_path"]) for row in rows))
        pieces = [
            OutfitPiece(image=image, category=SLOT_LABELS.get(row["slot"], "item"), label=" ".join(value for value in (row.get("color"), row["name"]) if value)[:120])
            for row, image in zip(rows, images)
        ]
        summary = [{"id": row["id"], "slot": row["slot"], "name": row["name"], "collection": row["collection"], "product_url": row.get("product_url")} for row in rows]
        return pieces, summary

    async def suggest(self, user_id: str, collection: str, occasion: str | None, count: int) -> list[OutfitSuggestion]:
        rows = await self._rows(user_id, None if collection == "all" else collection)
        rows = rows[:MAX_SUGGESTION_ITEMS]
        if len(rows) < 2:
            raise TryOnError("Add at least two items (for example a top and a bottom) before asking for outfit ideas", 400)
        parts: list[dict[str, Any]] = [{
            "text": (
                "You are a friendly personal stylist. Build complete, wearable outfits only from the wardrobe items below. "
                "Each item is shown as a photo followed by its id and description. "
                f"Suggest up to {count} different outfits{f' for this occasion: {occasion}' if occasion else ' for everyday wear'}. "
                "Each outfit uses either one top plus one bottom, or one dress; optionally add one jacket or layer, one footwear, "
                "and up to two jewelry or accessory items. Never use two items from the same category except jewelry and accessories. "
                "Consider colour harmony, formality, pattern mixing and the occasion. Prefer items marked owned when both fit equally well. "
                "Give each outfit a short title and a one or two sentence reason. Use only the exact ids given."
            ),
        }]
        limit = asyncio.Semaphore(8)

        async def thumbnail(row: dict[str, Any]) -> dict[str, Any] | None:
            async with limit:
                try:
                    return _thumbnail(await self.storage.download(row["image_path"]))
                except (TryOnError, OSError, httpx.HTTPError):
                    return None

        for row, image in zip(rows, await asyncio.gather(*(thumbnail(row) for row in rows))):
            if image:
                parts += [image, {"text": f"id {row['id']}: {_describe(row)}"}]
        result = await self.storage.generate_json(parts, SUGGESTION_SCHEMA)
        known = {row["id"]: row for row in rows}
        outfits: list[OutfitSuggestion] = []
        for outfit in (result or {}).get("outfits") or []:
            ids = [item_id for item_id in dict.fromkeys(outfit.get("item_ids") or []) if item_id in known][:MAX_OUTFIT_PIECES]
            if len(ids) >= 2:
                outfits.append(OutfitSuggestion(title=str(outfit.get("title") or "Outfit idea")[:120], reason=str(outfit.get("reason") or "")[:600], item_ids=ids))
        if not outfits:
            raise TryOnError("The stylist could not build an outfit from these items. Add a few more tops, bottoms or shoes and try again.", 422)
        return outfits[:count]

    async def _rows(self, user_id: str, collection: str | None = None, ids: list[str] | None = None) -> list[dict[str, Any]]:
        params = {"anonymous_user_id": f"eq.{user_id}", "select": "*", "order": "created_at.desc", "limit": str(MAX_WARDROBE_ITEMS)}
        if collection:
            params["collection"] = f"eq.{collection}"
        if ids is not None:
            for item_id in ids:
                try:
                    UUID(item_id)
                except ValueError as exc:
                    raise TryOnError("Invalid wardrobe item id", 400) from exc
            params["id"] = f"in.({','.join(ids)})"
        response = await self.storage.rest("GET", "wardrobe_items", params=params)
        if response.status_code >= 400:
            raise TryOnError(self._table_error(response), 503)
        return response.json()

    async def _to_items(self, rows: list[dict[str, Any]]) -> list[WardrobeItem]:
        signed = await self.storage.signed_urls([row["image_path"] for row in rows])
        return [
            WardrobeItem(
                id=row["id"], collection=row["collection"], slot=row["slot"], name=row["name"], brand=row.get("brand"),
                color=row.get("color"), price=row.get("price"), currency=row.get("currency"), sizes=row.get("sizes") or [],
                selected_size=row.get("selected_size"), store=row.get("store"), product_url=row.get("product_url"), notes=row.get("notes"),
                image_url=signed.get(row["image_path"], ""), created_at=datetime.fromisoformat(row["created_at"].replace("Z", "+00:00")),
            )
            for row in rows
        ]

    @staticmethod
    def _table_error(response: Any) -> str:
        if response.status_code == 404 or "PGRST205" in response.text or "does not exist" in response.text:
            return "The wardrobe is not set up yet. Run supabase/schema.sql in the Supabase SQL editor."
        return "Could not reach the wardrobe database"
