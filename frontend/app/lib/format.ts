/** Shared display formatters for cloud resource types. */

export const TYPE_LABELS: Record<string, string> = {
  "microsoft.compute/virtualmachines": "Virtual machines",
  "microsoft.compute/disks": "Managed disks",
  "microsoft.compute/snapshots": "Snapshots",
  "microsoft.web/serverfarms": "App Service plans",
  "microsoft.web/sites": "App Services",
  "microsoft.storage/storageaccounts": "Storage accounts",
  "microsoft.network/publicipaddresses": "Public IPs",
  "microsoft.network/networkinterfaces": "Network interfaces",
  "microsoft.network/loadbalancers": "Load balancers",
  "microsoft.network/bastionhosts": "Bastion hosts",
  "microsoft.containerregistry/registries": "Container registries",
  "microsoft.sql/servers": "SQL servers",
  "microsoft.containerservice/managedclusters": "AKS clusters",
};

/** Humanize an Azure resource type into a short label ("…/virtualMachines" → "Virtual machines"). */
export function prettyType(t?: string | null): string {
  if (!t) return "Other";
  const key = t.toLowerCase();
  if (TYPE_LABELS[key]) return TYPE_LABELS[key];
  const tail = key.split("/").pop() ?? key;
  return tail.charAt(0).toUpperCase() + tail.slice(1);
}

/**
 * Derive a humanized resource type from an Azure resource id by reading the
 * `…/providers/<namespace>/<type>/…` segment. Falls back to "Other" for ids that
 * don't carry a provider path. Lets us label recommendations by what KIND of
 * resource they touch (Bastion hosts, Managed disks, …), not just VMs.
 */
export function resourceTypeFromId(id?: string | null): string {
  if (!id) return "Other";
  const parts = id.split("/");
  const i = parts.findIndex((p) => p.toLowerCase() === "providers");
  if (i >= 0 && parts.length > i + 2) {
    return prettyType(`${parts[i + 1]}/${parts[i + 2]}`);
  }
  return "Other";
}
