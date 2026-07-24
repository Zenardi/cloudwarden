"use client";

import { useCallback, useEffect, useState } from "react";
import {
  getK8sClusters,
  getK8sNamespaces,
  getK8sRecommendations,
  K8sRecommendation,
  KubeCluster,
  money,
  NamespaceCost,
  shortId,
} from "../lib/api";

const CLOUDS = ["all", "aws", "azure", "gcp"] as const;

export default function Kubernetes() {
  const [provider, setProvider] = useState<string>("all");
  const [clusters, setClusters] = useState<KubeCluster[]>([]);
  const [namespaces, setNamespaces] = useState<NamespaceCost[]>([]);
  const [recs, setRecs] = useState<K8sRecommendation[]>([]);
  const [err, setErr] = useState("");
  const [loading, setLoading] = useState(false);

  const clusterName = useCallback(
    (cid: string) => clusters.find((c) => c.cluster_id === cid)?.name ?? shortId(cid),
    [clusters],
  );

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [cl, ns, rc] = await Promise.all([
        getK8sClusters(provider),
        getK8sNamespaces(provider),
        getK8sRecommendations(provider),
      ]);
      setClusters(cl);
      setNamespaces(ns);
      setRecs(rc);
      setErr("");
    } catch (e) {
      setErr(String(e));
      setClusters([]);
      setNamespaces([]);
      setRecs([]);
    } finally {
      setLoading(false);
    }
  }, [provider]);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <h1>Kubernetes</h1>
      <p className="sub">
        Managed-cluster (AKS/EKS/GKE) namespace cost allocation and workload right-sizing.
        Node cost is split across namespaces by requested resources; over-provisioned
        workloads and idle namespaces are flagged from observed usage (advisory).
      </p>

      <div className="field">
        <label htmlFor="k-cloud">Cloud</label>
        <select id="k-cloud" value={provider} onChange={(e) => setProvider(e.target.value)}>
          {CLOUDS.map((c) => (
            <option key={c} value={c}>
              {c === "all" ? "All clouds" : c}
            </option>
          ))}
        </select>
      </div>

      {err && <div className="err">{err}</div>}

      <h2>Clusters</h2>
      <table>
        <thead>
          <tr>
            <th>Name</th>
            <th>Cloud</th>
            <th>Region</th>
            <th>Version</th>
            <th className="num">Nodes</th>
            <th className="num">Node cost / mo</th>
          </tr>
        </thead>
        <tbody>
          {clusters.map((c) => (
            <tr key={c.cluster_id}>
              <td>{c.name}</td>
              <td>
                <span className="badge">{c.provider}</span>
              </td>
              <td>{c.region ?? "—"}</td>
              <td className="muted">{c.version ?? "—"}</td>
              <td className="num">{c.node_count}</td>
              <td className="num">{money(c.node_monthly_cost, c.currency)}</td>
            </tr>
          ))}
          {clusters.length === 0 && !err && (
            <tr>
              <td colSpan={6} className="muted">
                {loading ? "Loading…" : "No clusters discovered."}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <h2>Namespace cost (allocated)</h2>
      <table>
        <thead>
          <tr>
            <th>Cluster</th>
            <th>Namespace</th>
            <th className="num">CPU req (cores)</th>
            <th className="num">Mem req (GiB)</th>
            <th className="num">Share</th>
            <th className="num">Cost / mo</th>
          </tr>
        </thead>
        <tbody>
          {namespaces.map((n) => (
            <tr key={`${n.cluster_id}/${n.namespace}`}>
              <td>{clusterName(n.cluster_id)}</td>
              <td>{n.namespace}</td>
              <td className="num">{n.cpu_request}</td>
              <td className="num">{n.mem_request}</td>
              <td className="num">{(n.share * 100).toFixed(1)}%</td>
              <td className="num">{money(n.cost, n.currency)}</td>
            </tr>
          ))}
          {namespaces.length === 0 && !err && (
            <tr>
              <td colSpan={6} className="muted">
                {loading ? "Loading…" : "No namespace cost to show."}
              </td>
            </tr>
          )}
        </tbody>
      </table>

      <h2>Right-sizing &amp; idle recommendations</h2>
      <table>
        <thead>
          <tr>
            <th>Category</th>
            <th>Resource</th>
            <th>Recommended</th>
            <th className="num">Savings / mo</th>
            <th>Rationale</th>
          </tr>
        </thead>
        <tbody>
          {recs.map((r, i) => (
            <tr key={`${r.resource_id}-${i}`}>
              <td>
                <span className="badge">{r.category}</span>
              </td>
              <td className="muted" title={r.resource_id}>
                {shortId(r.resource_id)}
              </td>
              <td className="muted">{r.recommended_sku ?? "—"}</td>
              <td className="num">{money(r.est_monthly_savings, r.currency)}</td>
              <td className="muted">{r.rationale}</td>
            </tr>
          ))}
          {recs.length === 0 && !err && (
            <tr>
              <td colSpan={5} className="muted">
                {loading ? "Loading…" : "No recommendations."}
              </td>
            </tr>
          )}
        </tbody>
      </table>
    </>
  );
}
