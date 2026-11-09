import { z } from 'zod';
const maybeText = z.string().nullish();
const maybeNumber = z.number().nullish();
export const capabilityStateSchema = z.object({
  mode: z.enum(['read', 'write']),
  status: z.enum(['available', 'not-tested', 'unsupported', 'error']),
  attempt: z.enum(['verified', 'unverified', 'confirmed-only', 'blocked']),
  source: z.string(),
  evidence: z.string(),
  checked_at: z.string(),
});
const screenModesSchema = z.array(
  z.object({ width: z.number(), height: z.number(), orientation: z.string() }),
);
const themeSupportSchema = z.object({
  status: z.enum(['supported', 'unsupported']),
  reason: z.object({ en: z.string(), ru: z.string() }),
});
export const deviceSchema = z.object({
  id: z.string(),
  name: maybeText,
  model: maybeText,
  rm: maybeText,
  firmware: maybeText,
  transport: z.string(),
  connection_kind: maybeText,
  connected: z.boolean(),
  configured: z.boolean().optional(),
  battery: maybeNumber,
  signal: maybeNumber,
  free: maybeNumber,
  total: maybeNumber,
  imei: maybeText,
  evidence: maybeText,
  capabilities: z.array(z.string()),
  capability_matrix: z.record(z.string(), capabilityStateSchema).optional(),
  capability_fingerprint: z.string().optional(),
  failure: z.object({ reason: z.string() }).nullish(),
  /** Which route this row belongs to and what the Bridge made of it; no phone data, no interface path. */
  route: maybeText,
  route_state: maybeText,
  recommended_for: z.array(z.string()).optional(),
  route_preference: maybeText,
  dead_end: z.boolean().optional(),
  capabilities_checked_at: z.string().optional(),
  sms_send_verified: z.boolean().optional(),
  usb_mode: maybeText,
  life_timer_hours: maybeNumber,
  life_timer_reason: maybeText,
  capability_status: z
    .record(z.string(), z.object({ status: z.string(), reason: maybeText }))
    .optional(),
  model_profile: z
    .object({
      id: z.string().optional(),
      name: z.string(),
      name_ru: z.string().optional(),
      variant_known: z.boolean().optional(),
      identity_note: z.string().nullish(),
      image: z.string().nullish(),
      screen: z
        .object({ width: z.number(), height: z.number(), orientation: z.string() })
        .nullish(),
      s60: z.string(),
      symbian: maybeText,
      input: maybeText,
      screen_modes: screenModesSchema.optional(),
      theme_studio: themeSupportSchema.optional(),
      platform_family: z.string().optional(),
      os_releases: z.array(z.string()).optional(),
      image_views: z
        .array(z.object({ image: z.string(), en: z.string(), ru: z.string() }))
        .optional(),
      release_evidence: z
        .object({ value: z.string().nullable(), status: z.string(), source: z.string() })
        .optional(),
    })
    .nullish(),
  firmware_update: z
    .object({ status: z.string(), latest_known: maybeText, sources: z.array(z.string()) })
    .optional(),
});
export const connectionRowSchema = z.object({
  id: z.string(),
  connected: z.boolean().nullish(),
  failure: z.object({ reason: z.string() }).nullish(),
  model: maybeText,
  connection_kind: maybeText,
});
/** A phone model of the registry (symport/phone/device_models.json): the single source for artwork and geometry. */
export const modelSchema = z.object({
  id: z.string(),
  model: z.string(),
  name: z.string(),
  family: maybeText,
  image: maybeText,
  screen: z.object({ width: z.number(), height: z.number(), orientation: z.string() }).nullish(),
  input: maybeText,
  s60: z.string(),
  symbian: maybeText,
  os_releases: z.array(z.string()).optional(),
  image_views: z.array(z.object({ image: z.string(), en: z.string(), ru: z.string() })).optional(),
  toolchain_profile: maybeText,
  screen_modes: screenModesSchema.optional(),
  theme_studio: themeSupportSchema.optional(),
});
export type PhoneModel = z.infer<typeof modelSchema>;
export const discoverySchema = z.object({
  devices: z.array(deviceSchema),
  models: z.array(modelSchema).optional(),
  connections: z.array(connectionRowSchema).optional(),
  limitations: z.record(z.string(), z.string()),
});
export const filesSchema = z.object({
  entries: z.array(z.object({ name: z.string(), directory: z.boolean(), size: maybeNumber })),
});
export const downloadSchema = z.object({ name: z.string(), content: z.string() });
export const resultSchema = z.object({
  status: z.string().optional(),
  destination: z.string().optional(),
});
export const historySchema = z.object({
  operations: z.array(z.object({ time: z.string(), action: z.string(), status: z.string() })),
});
export const connectionSchema = z.object({
  mode: z.string(),
  connected: z.boolean(),
  relay_token: z.string().optional(),
  expires_in: z.number().optional(),
});
export type CapabilityState = z.infer<typeof capabilityStateSchema>;
export const recheckSchema = z.object({ device: deviceSchema });
export type ConnectionRow = z.infer<typeof connectionRowSchema>;
export type Device = z.infer<typeof deviceSchema>;
export type FileEntry = z.infer<typeof filesSchema>['entries'][number];
export type Json = string | number | boolean | null | Json[] | { [key: string]: Json };
