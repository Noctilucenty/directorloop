/** A browser preview is optional; its failure must not discard a selected upload. */
export function createLocalPreview(file: Blob, api: {createObjectURL?: (file: Blob) => string; revokeObjectURL?: (url: string) => void} | undefined = globalThis.URL) {
  let url: string | null = null;
  try { if (typeof api?.createObjectURL === 'function') url = api.createObjectURL(file); } catch { /* Upload remains available without a preview. */ }
  return {url, release() { if (url) { try { api?.revokeObjectURL?.(url); } catch { /* Cleanup cannot break the page. */ } url = null; } }};
}
