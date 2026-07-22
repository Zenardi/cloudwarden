/** Shared display formatters for cloud resource types. */

export const TYPE_LABELS: Record<string, string> = {
  "microsoft.compute/virtualmachines": "Virtual machines",
  "microsoft.compute/disks": "Managed disks",
  "microsoft.web/serverfarms": "App Service plans",
  "microsoft.storage/storageaccounts": "Storage accounts",
  "microsoft.network/publicipaddresses": "Public IPs",
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
