import React, { useState, useEffect, useRef } from 'react';
import { SunIcon, MoonIcon, MonitorIcon } from '@phosphor-icons/react';
import { formatTimeForDisplay } from '../utils';
import { INK_GRADIENTS } from '../constants';
import PrimaryButton from './PrimaryButton';
import LocationSearch from './widgets/LocationSearch';
import { adminAuthFetch } from '../lib/adminAuthFetch';
import PrinterStatusDot from './PrinterStatusDot';
import { indicatorLabel } from '../hooks/usePrinterLink';


const GeneralSettings = ({
  settings,
  saveGlobalSettings,
  onConfigImported,
  printerLink,
}) => {
  const inputClass = 'w-full p-3 text-base border-2 border-gray-300 rounded-lg focus:outline-none box-border';
  const labelClass = 'block mb-2 text-sm text-black font-bold';

  // Use shared ink gradients
  const inkGradients = INK_GRADIENTS;

  // Server time (read-only; the host or container owns the clock)
  const [currentTime, setCurrentTime] = useState(null);
  const [currentVersion, setCurrentVersion] = useState(null);

  const [configTransferMessage, setConfigTransferMessage] = useState({ type: '', message: '', warnings: [] });
  const [exportingConfig, setExportingConfig] = useState(false);
  const [importingConfig, setImportingConfig] = useState(false);
  const configFileInputRef = useRef(null);

  useEffect(() => {
    const fetchTime = async () => {
      try {
        const response = await fetch('/api/system/time');
        const data = await response.json();
        if (data.datetime) setCurrentTime(data);
      } catch (err) {
        console.error('Error fetching server time:', err);
      }
    };

    fetchTime();
    const interval = setInterval(fetchTime, 30000);
    return () => clearInterval(interval);
  }, []);

  useEffect(() => {
    fetch('/api/system/version')
      .then((response) => response.json())
      .then((data) => setCurrentVersion(data.version || null))
      .catch((err) => console.error('Error fetching version:', err));
  }, []);


  const getApiError = (data, fallback) => {
    return data?.detail || data?.error || data?.message || fallback;
  };

  const clearConfigTransferMessage = () => {
    setConfigTransferMessage({ type: '', message: '', warnings: [] });
  };

  const parseContentDispositionFilename = (headerValue) => {
    if (!headerValue) return '';
    const utf8Match = headerValue.match(/filename\*=UTF-8''([^;]+)/i);
    if (utf8Match?.[1]) {
      try {
        return decodeURIComponent(utf8Match[1]);
      } catch {
        return utf8Match[1];
      }
    }

    const filenameMatch = headerValue.match(/filename="?([^";]+)"?/i);
    return filenameMatch?.[1] || '';
  };

  const handleExportConfig = async () => {
    setExportingConfig(true);
      setConfigTransferMessage({ type: '', message: '', warnings: [] });

    try {
      const response = await adminAuthFetch('/api/settings/export');
      if (!response.ok) {
        const data = await response.json().catch(() => ({}));
        throw new Error(getApiError(data, 'Failed to export configuration'));
      }

      const blob = await response.blob();
      const filename =
        parseContentDispositionFilename(response.headers.get('Content-Disposition')) ||
        'pc1-config.json';
      const url = window.URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      window.URL.revokeObjectURL(url);

      setConfigTransferMessage({ type: 'success', message: 'Configuration exported.', warnings: [] });
    } catch (err) {
      setConfigTransferMessage({ type: 'error', message: err.message || 'Failed to export configuration', warnings: [] });
    } finally {
      setExportingConfig(false);
    }
  };

  const handleImportConfig = async (event) => {
    const file = event.target.files?.[0];
    event.target.value = '';

    if (!file) {
      return;
    }

    if (!window.confirm(`Import configuration from "${file.name}"? This will replace current settings, channels, and modules.`)) {
      return;
    }

    setImportingConfig(true);
    setConfigTransferMessage({ type: '', message: '', warnings: [] });

    try {
      const fileText = await file.text();

      const response = await adminAuthFetch('/api/settings/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: fileText,
      });
      const data = await response.json().catch(() => ({}));

      if (!response.ok) {
        throw new Error(getApiError(data, 'Failed to import configuration'));
      }

      onConfigImported?.(data.config, data.message || 'Configuration imported successfully!');

      const warnings = Array.isArray(data.validation_warnings) ? data.validation_warnings : [];
      const warningSuffix = warnings.length > 0
        ? ' Imported with validation warnings.'
        : '';
      setConfigTransferMessage({
        type: 'success',
        message: `${data.message || 'Configuration imported.'}${warningSuffix}`,
        warnings,
      });
    } catch (err) {
      setConfigTransferMessage({ type: 'error', message: err.message || 'Failed to import configuration', warnings: [] });
    } finally {
      setImportingConfig(false);
    }
  };

  return (
    <div className='space-y-4'>
      {/* Location Settings */}
      <div className='rounded-xl p-[4px] shadow-lg' style={{ background: inkGradients[2] }}>
        <div className='bg-bg-card rounded-lg p-4 flex flex-col'>
          <h3 className='font-bold text-black  text-lg tracking-tight mb-3'>Location</h3>

          {/* Universal Location Search Component */}
          <div className='mb-4'>
            <label className='block mb-2 text-sm text-black font-bold'>Set Location</label>
            <LocationSearch 
              value={settings} 
              onChange={(newLoc) => saveGlobalSettings(newLoc)} 
            />
          </div>
        </div>
      </div>

      {/* Time Settings */}
      <div className='rounded-xl p-[4px] shadow-lg' style={{ background: inkGradients[1] }}>
        <div className='bg-bg-card rounded-lg p-4 flex flex-col'>
          <h3 className='font-bold text-black  text-lg tracking-tight mb-3'>Time Settings</h3>

          {/* Time Format */}
          <div className='mb-4'>
            <label className='block mb-2 text-sm text-black font-bold'>Time Format</label>
            <select
              value={settings.time_format || '12h'}
              onChange={(e) => saveGlobalSettings({ time_format: e.target.value })}
              className={inputClass}>
              <option value='12h'>12-hour (3:45 PM)</option>
              <option value='24h'>24-hour (15:45)</option>
            </select>
            <p className='text-xs text-gray-600 mt-1 '>Choose how times are displayed across all modules</p>
          </div>

          <div>
            <label className='block mb-2 text-sm text-black font-bold'>Server Time</label>
            <div className='p-3 bg-gray-50 border-2 border-gray-300 rounded-lg'>
              <div className='text-lg font-bold text-black'>
                {currentTime
                  ? `${currentTime.date} ${formatTimeForDisplay(currentTime.time, settings.time_format || '12h')}`
                  : '—'}
              </div>
              <div className='text-xs text-gray-600 mt-1'>{currentTime?.timezone || settings.timezone}</div>
            </div>
            <p className='text-xs text-gray-600 mt-1'>
              Shown in the configured timezone. The clock comes from the host running the server; set the
              timezone through Location.
            </p>
          </div>
        </div>
      </div>

      {/* Appearance */}
      <div className='rounded-xl p-[4px] shadow-lg' style={{ background: inkGradients[4] }}>
        <div className='bg-bg-card rounded-lg p-4 flex flex-col'>
          <h3 className='font-bold text-black  text-lg tracking-tight mb-3'>Appearance</h3>
          <div className='flex flex-wrap items-center justify-between gap-4'>
            <p className='flex-1 min-w-40 text-sm text-gray-600 '>
              System follows the appearance setting of the device you're browsing from.
            </p>
            <div className='inline-flex rounded-full border-2 border-black bg-white p-1 shadow-sm'>
              {[
                { value: 'light', Icon: SunIcon },
                { value: 'dark', Icon: MoonIcon },
                { value: 'system', Icon: MonitorIcon },
              ].map((choice) => {
                const value = choice.value;
                const ThemeIcon = choice.Icon;
                const selected = (settings?.theme || 'light') === value;
                return (
                  <button
                    key={value}
                    type='button'
                    className={`flex items-center gap-1.5 rounded-full px-3 py-1 text-xs font-bold uppercase tracking-wide transition ${
                      selected
                        ? 'active-inverted'
                        : 'bg-transparent text-black hover:bg-gray-100'
                    }`}
                    aria-pressed={selected}
                    onClick={() => {
                      if (selected) return;
                      saveGlobalSettings({ theme: value });
                    }}
                  >
                    <ThemeIcon className='w-3.5 h-3.5' weight='bold' />
                    {value}
                  </button>
                );
              })}
            </div>
          </div>
        </div>
      </div>

      {/* Printer Settings */}
      <div className='rounded-xl p-[4px] shadow-lg' style={{ background: inkGradients[3] }}>
        <div className='bg-bg-card rounded-lg p-4 flex flex-col'>
          <h3 className='font-bold text-black  text-lg tracking-tight mb-3'>Printer Settings</h3>
          <div className='mb-4' data-testid='printer-link'>
            <label className={labelClass}>Printer Connection</label>
            <div className='flex items-center gap-2 text-sm text-black'>
              <PrinterStatusDot link={printerLink} />
              <span className='font-bold'>{indicatorLabel(printerLink)}</span>
            </div>
            {printerLink?.output_mode === 'esp32' && (
              <div className='text-xs text-gray-600 mt-1 space-y-0.5'>
                {!printerLink.available && <p>{printerLink.reason}</p>}
                {printerLink.device_id && (
                  <p>
                    {printerLink.device_id}
                    {printerLink.firmware ? ` · firmware ${printerLink.firmware}` : ''}
                    {printerLink.heartbeat_age_s != null ? ` · last heartbeat ${Math.round(printerLink.heartbeat_age_s)} s ago` : ''}
                  </p>
                )}
                {printerLink.last_job && (
                  <p>
                    Last receipt: {printerLink.last_job.state}
                    {printerLink.last_job.error ? ` (${printerLink.last_job.error})` : ''}
                  </p>
                )}
              </div>
            )}
          </div>
          <div>
            <label className={labelClass}>Maximum Print Lines</label>
            <input
              type='number'
              min='0'
              max='1000'
              value={settings.max_print_lines ?? 200}
              onChange={(e) => saveGlobalSettings({ max_print_lines: parseInt(e.target.value) || 0 })}
              className={inputClass}
            />
            <p className='text-xs text-gray-600 mt-1 '>
              Maximum lines per print job to prevent endless prints. Set to 0 for no limit (default: 200)
            </p>
          </div>
        </div>
      </div>

      {/* Config Backup */}
      <div className='rounded-xl p-[4px] shadow-lg' style={{ background: inkGradients[4] }}>
        <div className='bg-bg-card rounded-lg p-4 flex flex-col'>
          <h3 className='font-bold text-black text-lg tracking-tight mb-3'>Config Backup</h3>
          <p className='text-sm text-gray-600 mb-4'>
            Export your full PC-1 configuration to a JSON file, or import a previously exported config to restore settings, channels, and modules.
          </p>

          <input
            ref={configFileInputRef}
            type='file'
            accept='application/json,.json'
            onChange={handleImportConfig}
            className='hidden'
          />

          <div className='flex flex-col sm:flex-row gap-3'>
            <PrimaryButton
              onClick={handleExportConfig}
              disabled={exportingConfig || importingConfig}
              loading={exportingConfig}
              className='flex-1'>
              Export Config
            </PrimaryButton>
            <PrimaryButton
              onClick={() => configFileInputRef.current?.click()}
              disabled={exportingConfig || importingConfig}
              loading={importingConfig}
              className='flex-1'>
              Import Config
            </PrimaryButton>
          </div>

          <p className='text-xs text-gray-600 mt-2'>
            Imports replace the current configuration immediately. Use exported JSON files from PC-1 for the safest restore path.
          </p>

          {configTransferMessage.message && (
            <div
              className={`mt-4 p-3 rounded-lg text-sm border-2 ${
                configTransferMessage.type === 'success'
                  ? 'bg-gray-100 text-black border-black'
                  : 'bg-white text-black border-black border-dashed'
              }`}>
              <div className='flex items-start justify-between gap-3'>
                <div className='flex-1'>
                  {configTransferMessage.type === 'error' && <span className='font-bold mr-2'>ERROR:</span>}
                  <span>{configTransferMessage.message}</span>
                </div>
                <button
                  type='button'
                  onClick={clearConfigTransferMessage}
                  className='shrink-0 rounded border border-gray-400 bg-white px-2 py-1 text-xs font-bold text-gray-700 hover:border-black hover:text-black transition-colors cursor-pointer'>
                  X
                </button>
              </div>
              {configTransferMessage.warnings?.length > 0 && (
                <div className='mt-3 space-y-2'>
                  {configTransferMessage.warnings.map((warning, index) => (
                    <div
                      key={`${warning}-${index}`}
                      className='rounded-lg border border-gray-300 bg-white px-3 py-2 text-xs text-gray-700'>
                      {warning}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {/* Version */}
      <div className='rounded-xl p-[4px] shadow-lg' style={{ background: inkGradients[1] }}>
        <div className='bg-bg-card rounded-lg p-4 flex flex-col'>
          <h3 className='font-bold text-black text-lg tracking-tight mb-1'>Version</h3>
          <p className='text-sm text-gray-600 font-mono'>{currentVersion || 'unknown'}</p>
          <p className='text-xs text-gray-600 mt-1'>Update by deploying a new container image.</p>
        </div>
      </div>
    </div>
  );
};

export default GeneralSettings;
