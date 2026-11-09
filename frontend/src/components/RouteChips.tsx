import { usePreferences } from '../state/app';
import type { Device } from '../api/contracts';
import { JOB_TEXT, ROUTE_TEXT, STATE_TEXT, recommended, routeOf, stateOf } from '../wizard/routes';

/** Route name, its state and (when the Bridge recommends it) “Recommended” with the jobs it is recommended for. */
export default function RouteChips({ device }: { device: Device }) {
  const { t } = usePreferences();
  const route = routeOf(device);
  if (!route) return null;
  const state = stateOf(device);
  return (
    <span className="route-chips">
      <span className="chip" data-route={route} data-state={state}>
        {t(...ROUTE_TEXT[route])} · {t(...(STATE_TEXT[state] ?? [state, state]))}
      </span>
      {recommended(device) && (
        <span className="chip route-recommended">
          {t('Recommended', 'Рекомендуется')}:{' '}
          {(device.recommended_for ?? [])
            .map((job) => t(...(JOB_TEXT[job] ?? [job, job])))
            .join(', ')}
        </span>
      )}
    </span>
  );
}
