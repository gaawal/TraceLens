import { useRef } from 'react';
import { CalendarDays } from 'lucide-react';

function pad(value: number): string { return String(value).padStart(2, '0'); }

function formatLocalDate(date: Date): string {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

function parseInputDate(value: string): Date | undefined {
  const normalized = value.trim().replace('T', ' ');
  const match = normalized.match(/^(\d{4})-(\d{2})-(\d{2})\s+(\d{2}):(\d{2})(?::(\d{2}))?(?:\.(\d{1,6}))?$/);
  if (!match) return undefined;
  const [, year, month, day, hour, minute, second = '0', fraction = ''] = match;
  const date = new Date(Number(year), Number(month) - 1, Number(day), Number(hour), Number(minute), Number(second), Number(fraction.padEnd(3, '0').slice(0, 3) || '0'));
  return Number.isNaN(date.getTime()) ? undefined : date;
}

function toNativePickerValue(value: string): string {
  const date = parseInputDate(value);
  if (!date) return '';
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function SmartDateTimeInput({ value, label, onChange, disabled = false }: { value: string; label: string; onChange: (value: string) => void; disabled?: boolean }) {
  const pickerRef = useRef<HTMLInputElement>(null);

  function openPicker() {
    const input = pickerRef.current;
    if (!input || disabled) return;
    input.value = toNativePickerValue(value);
    if (typeof input.showPicker === 'function') input.showPicker(); else input.click();
  }

  return (
    <label className={disabled ? "remote-smart-time disabled" : "remote-smart-time"}>
      <span>{label}</span>
      <div>
        <input type="text" value={value} title={value} disabled={disabled} onChange={(event) => onChange(event.target.value.replace('T', ' '))} placeholder="YYYY-MM-DD HH:mm:ss" />
        <button type="button" onClick={openPicker} disabled={disabled} title={`选择${label}`}><CalendarDays size={14} /></button>
        <input
          ref={pickerRef}
          type="datetime-local"
          step="1"
          tabIndex={-1}
          aria-hidden="true"
          disabled={disabled}
          className="remote-native-time-picker"
          onChange={(event) => {
            const date = new Date(event.target.value);
            if (!Number.isNaN(date.getTime())) onChange(formatLocalDate(date));
          }}
        />
      </div>
    </label>
  );
}
