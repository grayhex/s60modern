import { describe, it, expect, vi } from 'vitest';
import { ApiClient, ApiError } from '../api/client';
describe('API boundary', () => {
  it('uses headers, keeps tokens out of URLs and validates responses', async () => {
    const fetch = vi
      .fn()
      .mockResolvedValue(new Response(JSON.stringify({ devices: [], limitations: {} })));
    vi.stubGlobal('fetch', fetch);
    const client = new ApiClient(() => 'private');
    await expect(client.discover()).resolves.toEqual({ devices: [], limitations: {} });
    expect(fetch).toHaveBeenCalledWith(
      '/api/manager',
      expect.objectContaining({
        headers: expect.objectContaining({ 'X-Symport-Token': 'private' }),
        cache: 'no-store',
      }),
    );
  });
  it('rejects unexpected payloads instead of inventing device data', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('{}')));
    await expect(new ApiClient(() => 'token').discover()).rejects.toBeInstanceOf(ApiError);
  });
  it('does not retry an uncertain write', async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError('Disconnected'));
    vi.stubGlobal('fetch', fetch);
    await expect(new ApiClient(() => 'token').fileAction('delete', 'card', 'x')).rejects.toThrow(
      'Disconnected',
    );
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
describe('Home LAN sign-in', () => {
  it('reloads once to show the login screen when the session is gone', async () => {
    const meta = document.createElement('meta');
    meta.name = 'symport-auth';
    meta.content = 'lan';
    document.head.append(meta);
    sessionStorage.clear();
    const reload = vi.fn();
    vi.stubGlobal('location', { reload, replace: vi.fn() });
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation(
        async () =>
          new Response(JSON.stringify({ error: 'Sign in required', login: true }), {
            status: 401,
          }),
      ),
    );
    const client = new ApiClient(() => 'csrf');
    await expect(client.discover()).rejects.toBeInstanceOf(ApiError);
    await expect(client.discover()).rejects.toBeInstanceOf(ApiError);
    expect(reload).toHaveBeenCalledTimes(1);
    meta.remove();
  });
  it('signs out with the CSRF header and returns to the login screen', async () => {
    const replace = vi.fn();
    vi.stubGlobal('location', { reload: vi.fn(), replace });
    const fetch = vi.fn().mockResolvedValue(new Response('{"ok":true}'));
    vi.stubGlobal('fetch', fetch);
    await new ApiClient(() => 'csrf').logout();
    expect(fetch).toHaveBeenCalledWith(
      '/api/auth/logout',
      expect.objectContaining({
        method: 'POST',
        headers: expect.objectContaining({ 'X-Symport-Token': 'csrf' }),
      }),
    );
    expect(replace).toHaveBeenCalledWith('/');
  });
});
