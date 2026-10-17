import { indicatorLabel } from '../hooks/usePrinterLink';

// Fixed traffic-light colours: theme overrides remap Tailwind red/amber utilities.
const LEVEL_COLORS = {
  green: '#22c55e',
  amber: '#f59e0b',
  red: '#ef4444',
};
const UNKNOWN_COLOR = '#9ca3af';

// Visual-only summary: green ready, amber printing/degraded, red unavailable.
// Troubleshooting belongs to the container logs, ESP32 web UI and serial port.
export default function PrinterStatusDot({ link, className = '' }) {
  const label = indicatorLabel(link);
  const level = link?.indicator?.level;
  return (
    <span
      role='img'
      aria-label={`Printer status: ${label}`}
      title={label}
      data-testid='printer-status-dot'
      data-level={level || 'unknown'}
      className={`inline-block w-3.5 h-3.5 rounded-full shrink-0 ${className}`}
      style={{ backgroundColor: LEVEL_COLORS[level] || UNKNOWN_COLOR }}
    />
  );
}
