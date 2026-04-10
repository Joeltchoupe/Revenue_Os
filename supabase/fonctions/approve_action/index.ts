// supabase/functions/approve_action/index.ts
// Handles approval actions from token URLs (Slack/Email buttons)
// No Supabase Auth required — one-time token is the credential

import { serve } from "https://deno.land/std@0.168.0/http/server.ts"
import { createClient } from 'https://esm.sh/@supabase/supabase-js@2'

serve(async (req) => {
  const headers = {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Headers': 'content-type',
  }

  if (req.method === 'OPTIONS') return new Response('ok', { headers })

  try {
    const url   = new URL(req.url)
    const token  = url.searchParams.get('token') || (await req.json().catch(() => ({}))).token
    const action = url.searchParams.get('action') || (await req.json().catch(() => ({}))).action
    // action: approve | reject | snooze
    const snoozeHours = parseInt(url.searchParams.get('hours') || '24')

    if (!token || !action) {
      return new Response(JSON.stringify({ error: "Missing token or action" }), { status: 400, headers })
    }

    if (!['approve', 'reject', 'snooze'].includes(action)) {
      return new Response(JSON.stringify({ error: "Invalid action" }), { status: 400, headers })
    }

    const sb = createClient(
      Deno.env.get('SUPABASE_URL')!,
      Deno.env.get('SUPABASE_SERVICE_ROLE_KEY')!
    )

    // Fetch approval by token
    const { data: approval, error } = await sb
      .from('approvals')
      .select('*')
      .eq('token', token)
      .single()

    if (error || !approval) {
      // HTML response for browser redirect
      return new Response(_htmlPage('⚠️ Invalid Link', 'This link is not valid or has already been used.'), {
        status: 404, headers: { ...headers, 'Content-Type': 'text/html' }
      })
    }

    // Terminal state check
    if (['approved', 'rejected'].includes(approval.status)) {
      return new Response(_htmlPage('Already Actioned', `This request was already ${approval.status}.`), {
        status: 200, headers: { ...headers, 'Content-Type': 'text/html' }
      })
    }

    // Expiry check
    if (new Date(approval.expires_at) < new Date()) {
      await sb.from('approvals').update({ status: 'expired' }).eq('id', approval.id)
      return new Response(_htmlPage('⏰ Link Expired', 'This approval link has expired. You can approve from your dashboard.'), {
        status: 410, headers: { ...headers, 'Content-Type': 'text/html' }
      })
    }

    const now = new Date().toISOString()
    const newStatus = action === 'approve' ? 'approved' : action === 'reject' ? 'rejected' : 'snoozed'
    const update: Record<string, unknown> = { status: newStatus, actioned_at: now }

    if (newStatus === 'snoozed') {
      const snoozeUntil = new Date(Date.now() + snoozeHours * 60 * 60 * 1000).toISOString()
      update['snooze_until'] = snoozeUntil
    }

    // Update approval
    await sb.from('approvals').update(update).eq('id', approval.id)

    // Update recommendation
    if (approval.recommendation_id) {
      await sb.from('recommendations').update({
        status: newStatus, updated_at: now
      }).eq('id', approval.recommendation_id)

      // Set first_acted_at only once
      await sb.from('recommendations').update({ first_acted_at: now })
        .eq('id', approval.recommendation_id)
        .is('first_acted_at', null)
    }

    // Write execution event for n8n (if approved)
    if (newStatus === 'approved') {
      await sb.from('events').insert({
        tenant_id:  approval.tenant_id,
        event_type: 'approval.approved',
        source:     'edge_function_token',
        payload: {
          approval_id:       approval.id,
          recommendation_id: approval.recommendation_id,
          action_type:       approval.action_type,
          payload:           approval.payload,
          correlation_id:    approval.correlation_id
        },
        processed: false
      })
    }

    // Audit log
    await sb.from('audit_logs').insert({
      tenant_id:     approval.tenant_id,
      actor:         'founder_token',
      action:        `approval_${newStatus}`,
      resource_type: 'approval',
      resource_id:   approval.id,
      data:          { action_type: approval.action_type, channel: approval.channel, token_used: true }
    })

    // Metrics
    await sb.rpc('increment_metric', { p_tenant_id: approval.tenant_id, p_field: 'approvals_actioned' })
    if (newStatus === 'approved') await sb.rpc('increment_metric', { p_tenant_id: approval.tenant_id, p_field: 'approvals_approved' })
    if (newStatus === 'rejected') await sb.rpc('increment_metric', { p_tenant_id: approval.tenant_id, p_field: 'approvals_rejected' })

    const titles = { approved: '✅ Approved', rejected: '❌ Rejected', snoozed: '⏰ Snoozed' }
    const bodies = {
      approved: 'The action has been approved and will be executed shortly.',
      rejected: 'The action has been rejected and will not be executed.',
      snoozed:  `This item has been snoozed for ${snoozeHours} hours.`
    }

    return new Response(_htmlPage(titles[newStatus], bodies[newStatus]), {
      status: 200, headers: { ...headers, 'Content-Type': 'text/html' }
    })

  } catch (err) {
    console.error('approve_action error:', err)
    return new Response(_htmlPage('Error', 'Something went wrong. Please try again from your dashboard.'), {
      status: 500, headers: { ...headers, 'Content-Type': 'text/html' }
    })
  }
})

function _htmlPage(title: string, body: string): string {
  return `<!DOCTYPE html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>${title} — Revenue OS</title>
<style>body{font-family:Arial,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;background:#f9fafb;}
.card{background:#fff;border-radius:12px;padding:40px 48px;text-align:center;box-shadow:0 4px 24px rgba(0,0,0,0.08);max-width:440px;}
h1{font-size:24px;color:#1a1a2e;margin:0 0 12px;}p{color:#6b7280;font-size:15px;margin:0 0 24px;line-height:1.6;}
a{background:#1a1a2e;color:#fff;padding:10px 22px;border-radius:6px;text-decoration:none;font-size:14px;}</style></head>
<body><div class="card"><h1>${title}</h1><p>${body}</p><a href="javascript:window.close()">Close this window</a></div></body></html>`
}
