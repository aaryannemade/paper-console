import { useEffect, useState } from 'react';
import { adminAuthFetch } from '../lib/adminAuthFetch';

const POLL_INTERVAL_MS = 5000;

// Polls /api/printer/link while `enabled` (i.e. after login); null until loaded.
export function usePrinterLink(enabled = true) {
  const [link, setLink] = useState(null);

  useEffect(() => {
    if (!enabled) {
      setLink(null);
      return undefined;
    }
    let cancelled = false;
    let timer;
    const fetchLink = async () => {
      try {
        const response = await adminAuthFetch('/api/printer/link', { signal: AbortSignal.timeout(5000) });
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!cancelled) setLink(data);
      } catch (err) {
        console.error('Error fetching printer link:', err);
        if (!cancelled) {
          setLink(previous => ({ ...previous, health_stale: true, indicator: { level: 'red', label: 'Printer status unavailable' } }));
        }
      } finally {
        if (!cancelled) timer = setTimeout(fetchLink, POLL_INTERVAL_MS);
      }
    };

    fetchLink();
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [enabled]);

  return link;
}

export function indicatorLabel(link) {
  return link?.indicator?.label || 'Checking printer status…';
}
