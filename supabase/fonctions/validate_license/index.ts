// supabase/functions/validate_license/index.ts
// Called by n8n at the start of every critical workflow
// Returns: { valid: boolean, tenant_id, plan, entitlements, reason? }

import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'
import { crypto } from "https://deno.land/std@0.168.0/crypto/mod.ts"

const corsHeaders = {
  'Access-Control-Allow-Origin': '*',
  'Access-Control-Allow-Headers': 'authorization, x-client-info, apikey, content-type, x-license-token',
}

serve(async (req) => {
  if (req.method === 'OPTIONS') return new Response('ok', { headers: corsHeaders })

  try {
    const body = await req.json()
    const { tenant_id, license_token } = body

    if (!tenant_id || !license_token) {
      return new Response(JSON.stringify({
        valid: false, reason: "Missing tenant_id or license_token"
      }), { status: 400, headers: { ...corsHeaders, 'Content-Type': 'application/json' } })
    }

    const sb = createClient(
      Deno.env.get('SUPABASE_URL')!,
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
    )

    // Hash the incoming token
    const encoder = new TextEncoder()
    const tokenData = encoder.encode(license_token)
    const hashBuffer = await crypto.subtle.digest('SHA-256', tokenData)
    const hashArray = Array.from(new Uint8Array(hashBuffer))
    const tokenHash = hashArray.map(b => b.toString(16).padStart(2, '0')).join('')

    // Fetch license
    const { data: license, error } = await sb
      .from('licenses')
      .select('id, tenant_id, plan, status, expires_at, entitlements')
      .eq('tenant_id', tenant_id)
      .eq('key_hash', tokenHash)
      .single()

    if (error || !license) {
      return new Response(JSON.stringify({
        valid: false, reason: "License not found"
      }), { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } })
    }

    // Check status
    if (license.status !== 'active' && license.status !== 'trial') {
      return new Response(JSON.stringify({
        valid: false, reason: `License is ${license.status}`, tenant_id, plan: license.plan
      }), { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } })
    }

    // Check expiry
    if (license.expires_at) {
      const expiresAt = new Date(license.expires_at)
      if (expiresAt < new Date()) {
        // Auto-update to expired
        await sb.from('licenses').update({ status: 'expired' }).eq('id', license.id)
        return new Response(JSON.stringify({
          valid: false, reason: "License expired", tenant_id, plan: license.plan
        }), { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } })
      }
    }

    // Check tenant status
    const { data: tenant } = await sb
      .from('tenants')
      .select('status')
      .eq('id', tenant_id)
      .single()

    if (tenant?.status === 'suspended') {
      return new Response(JSON.stringify({
        valid: false, reason: "Tenant is suspended", tenant_id
      }), { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } })
    }

    return new Response(JSON.stringify({
      valid: true,
      tenant_id,
      plan: license.plan,
      entitlements: license.entitlements,
      expires_at: license.expires_at
    }), { status: 200, headers: { ...corsHeaders, 'Content-Type': 'application/json' } })

  } catch (err) {
    console.error('validate_license error:', err)
    return new Response(JSON.stringify({
      valid: false, reason: "Internal error — treat as invalid"
    }), { status: 500, headers: { ...corsHeaders, 'Content-Type': 'application/json' } })
  }
})
