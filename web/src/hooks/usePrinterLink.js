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
    const fetchLink = async () => {
      try {
        const response = await adminAuthFetch('/api/printer/link');
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const data = await response.json();
        if (!cancelled) setLink(data);
      } catch (err) {
        console.error('Error fetching printer link:', err);
        if (!cancelled) {
          setLink({ indicator: { level: 'red', label: 'Printer status unavailable' } });
        }
      }
    };

    fetchLink();
    const interval = setInterval(fetchLink, POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(interval);
    };
  }, [enabled]);

  return link;
}

export function indicatorLabel(link) {
  return link?.indicator?.label || 'Checking printer status…';
}
