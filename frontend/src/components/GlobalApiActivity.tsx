import { useEffect, useState } from 'react';
import { LoaderCircle } from 'lucide-react';

export function GlobalApiActivity() {
  const [pending, setPending] = useState(0);
  const [lastPath, setLastPath] = useState('');

  useEffect(() => {
    const start = (event: Event) => {
      const detail = (event as CustomEvent<{ path?: string }>).detail;
      setLastPath(detail?.path || '');
      setPending((value) => value + 1);
    };
    const finish = () => setPending((value) => Math.max(0, value - 1));
    window.addEventListener('tracelens:api-start', start as EventListener);
    window.addEventListener('tracelens:api-finish', finish as EventListener);
    return () => {
      window.removeEventListener('tracelens:api-start', start as EventListener);
      window.removeEventListener('tracelens:api-finish', finish as EventListener);
    };
  }, []);

  if (!pending) return null;
  return (
    <div className="global-api-activity" role="status" aria-live="polite">
      <div className="global-api-progress" />
      <div className="global-api-chip"><LoaderCircle className="spin" size={14} /><span>读取中</span><small>{pending > 1 ? `${pending} 个请求` : lastPath.replace(/^\/|\?.*$/g, '')}</small></div>
    </div>
  );
}
