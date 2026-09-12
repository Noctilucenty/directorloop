export function JsonViewer({ value, title }: { value: unknown; title?: string }) {
  return (
    <details className="details" open={false}>
      <summary>{title ?? "JSON"}</summary>
      <pre className="json mono">{JSON.stringify(value, null, 2)}</pre>
    </details>
  );
}
