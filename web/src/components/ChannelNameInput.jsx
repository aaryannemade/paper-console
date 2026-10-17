import { useState } from 'react';

// Keep in sync with CHANNEL_NAME_MAX_LENGTH in app/config.py.
const CHANNEL_NAME_MAX_LENGTH = 24;

// Inline channel label: saves on blur or Enter, Escape restores the saved name.
// Render with key={name} so an external change (save, swap, import) resets the draft.
export default function ChannelNameInput({ position, name, onRename }) {
  const [draft, setDraft] = useState(name || '');

  const commit = () => {
    const next = draft.replace(/\s+/g, ' ').trim();
    if (next !== (name || '')) onRename(position, next);
    else setDraft(name || '');
  };

  return (
    <input
      type='text'
      value={draft}
      maxLength={CHANNEL_NAME_MAX_LENGTH}
      placeholder='Add name'
      aria-label={`Name for channel ${position}`}
      title='Channel name (shown on receipts and the device)'
      data-testid={`channel-name-${position}`}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === 'Enter') e.currentTarget.blur();
        if (e.key === 'Escape') {
          setDraft(name || '');
          // Blur after the reset so commit sees the restored value.
          requestAnimationFrame(() => e.target.blur());
        }
      }}
      className='min-w-0 flex-1 bg-transparent font-bold text-lg tracking-tight px-1 py-0 rounded border-2 border-transparent hover:border-gray-300 focus:border-black focus:outline-none placeholder:font-normal placeholder:text-sm'
      style={{ backgroundColor: 'transparent' }}
    />
  );
}
