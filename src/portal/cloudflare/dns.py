"""Zone resolution and CNAME upsert/delete, with an ownership check.

portal stamps every record it creates with a comment of the form
"managed by portal:<slug>". It refuses to touch a record that does not carry
that stamp, so a hand-made record is never clobbered.
"""

from __future__ import annotations

from ..errors import CloudflareError


def ownership_comment(slug: str) -> str:
    return f"managed by portal:{slug}"


def is_portal_owned(record: dict, slug: str) -> bool:
    return (record.get("comment") or "").strip() == ownership_comment(slug)


def candidate_zones(fqdn: str) -> list[str]:
    """Suffixes of the hostname that could be a zone, longest first.

    app.staging.example.com -> staging.example.com, example.com, com
    (the single-label tail is never a zone in practice but costs one lookup).
    """
    labels = fqdn.strip(".").split(".")
    return [".".join(labels[i:]) for i in range(1, len(labels))] or [fqdn]


def resolve_zone(client, fqdn: str) -> dict:
    """Walk suffixes of the fqdn until one is a zone in the account."""
    for name in candidate_zones(fqdn):
        results = client.get("/zones", params={"name": name})["result"]
        if results:
            return results[0]
    raise CloudflareError(
        f"No Cloudflare zone in this account matches '{fqdn}'. Check the domain "
        f"is on Cloudflare and the token has Zone:Read for it."
    )


def find_record(client, zone_id: str, fqdn: str) -> dict | None:
    results = client.get(
        f"/zones/{zone_id}/dns_records", params={"name": fqdn, "per_page": 50}
    )["result"]
    return results[0] if results else None


def tunnel_target(tunnel_id: str) -> str:
    return f"{tunnel_id}.cfargotunnel.com"


def upsert_cname(client, zone_id: str, fqdn: str, tunnel_id: str, slug: str) -> str:
    """Create or update the proxied CNAME for this hostname. Returns record id.

    Refuses to overwrite any existing record that portal did not create for this
    same slug.
    """
    payload = {
        "type": "CNAME",
        "name": fqdn,
        "content": tunnel_target(tunnel_id),
        "proxied": True,
        "ttl": 1,
        "comment": ownership_comment(slug),
    }

    existing = find_record(client, zone_id, fqdn)
    if existing is None:
        return client.post(f"/zones/{zone_id}/dns_records", json=payload)["result"]["id"]

    if not is_portal_owned(existing, slug):
        raise CloudflareError(
            f"A DNS record for {fqdn} already exists ({existing.get('type')} -> "
            f"{existing.get('content')}) and is not marked as managed by "
            f"portal:{slug}. Refusing to overwrite it. Remove it by hand, or "
            f"pick a different --host."
        )

    return client.put(
        f"/zones/{zone_id}/dns_records/{existing['id']}", json=payload
    )["result"]["id"]


def delete_record(client, zone_id: str, record_id: str, slug: str) -> bool:
    """Delete only if the record still carries this slug's ownership stamp."""
    try:
        record = client.get(f"/zones/{zone_id}/dns_records/{record_id}")["result"]
    except CloudflareError:
        return False  # already gone

    if not is_portal_owned(record, slug):
        raise CloudflareError(
            f"DNS record {record.get('name')} is no longer marked as managed by "
            f"portal:{slug}. Refusing to delete it; remove it by hand if you "
            f"really want it gone."
        )

    client.delete(f"/zones/{zone_id}/dns_records/{record_id}")
    return True
