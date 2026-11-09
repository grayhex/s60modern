import { useMutation, useQueryClient } from '@tanstack/react-query';
import { z } from 'zod';
import { useApi, usePreferences } from '../state/app';
import { keys, useDevices } from '../api/queries';
import { routeBanner } from '../wizard/routes';
import { ErrorMessage } from './ui';

/**
 * Contacts / calendar pages: tells the user which route reads this store and offers to switch. Switching only changes
 * the selected connection in the app (and writes one line to the operations journal); nothing is sent to the phone.
 */
export default function RouteBanner({ domain }: { domain: string }) {
  const api = useApi();
  const cache = useQueryClient();
  const { t, device, setDevice } = usePreferences();
  const devices = useDevices();
  const choose = useMutation({
    mutationFn: async (id: string) => {
      await api.request(
        '/api/manager',
        { action: 'connection-select', device: id },
        z.object({ status: z.string() }),
      );
      return id;
    },
    onSuccess: async (id) => {
      setDevice(id);
      await cache.invalidateQueries({ queryKey: keys.devices });
    },
  });
  const banner = routeBanner(devices.data?.devices ?? [], device, domain);
  if (!banner) return null;
  const text =
    banner.kind === 'using'
      ? t(
          'Using USB OBEX: contacts and calendar are read-only on this route.',
          'Используется USB OBEX: контакты и календарь на этом маршруте только для чтения.',
        )
      : banner.deadEnd
        ? t(
            'Gammu could not reach the phone; contacts and calendar are available over USB OBEX.',
            'Gammu не достучался до телефона, контакты и календарь доступны по USB OBEX.',
          )
        : t(
            'Contacts and calendar are available over USB OBEX (read-only).',
            'Контакты и календарь доступны по USB OBEX (только чтение).',
          );
  const target = banner.kind === 'using' ? banner.back : banner.target;
  return (
    <div className="card route-banner" role="status">
      <p>{text}</p>
      {target && (
        <button
          type="button"
          className="btn secondary"
          disabled={choose.isPending}
          onClick={() => choose.mutate(target.id)}
        >
          {banner.kind === 'using'
            ? t('Return to Gammu', 'Вернуться к Gammu')
            : t('Switch', 'Переключиться')}
        </button>
      )}
      <ErrorMessage error={choose.error} />
    </div>
  );
}
