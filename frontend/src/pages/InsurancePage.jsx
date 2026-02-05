/**
 * InsurancePage - Manage insurance policies and claims
 */

import React, { useState, useEffect, useCallback } from 'react';
import { useGuilds } from '../hooks/useGuilds';
import { insurance } from '../lib/api';
import { Card, CardContent, CardHeader, CardTitle, Badge, Button, Modal, useToast } from '../components/ui';
import { CreatePolicyForm } from '../components/forms';

const COVERAGE_LABELS = {
  xanax_stack: 'Xanax Stack Only',
  ecstasy_after_stack: 'Ecstasy After Stack',
  all_drugs: 'All Drug Losses',
};

const CLAIM_STATUS_LABELS = {
  pending: 'Pending',
  approved: 'Approved',
  rejected: 'Rejected',
  paid: 'Paid',
};

export default function InsurancePage() {
  const { selectedGuildId, selectedGuild } = useGuilds();
  const { addToast } = useToast();
  const [activeTab, setActiveTab] = useState('policies');
  const [policies, setPolicies] = useState([]);
  const [claims, setClaims] = useState([]);
  const [providers, setProviders] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showCreateModal, setShowCreateModal] = useState(false);

  const loadPolicies = useCallback(async () => {
    if (!selectedGuildId) return;
    setLoading(true);
    try {
      const data = await insurance.listPolicies({ guild_id: selectedGuildId });
      setPolicies(data.policies || []);
    } catch (err) {
      setError(err.message);
      addToast({ title: 'Failed to load policies', description: err.message, variant: 'error' });
    } finally {
      setLoading(false);
    }
  }, [selectedGuildId, addToast]);

  const loadClaims = useCallback(async () => {
    setLoading(true);
    try {
      const data = await insurance.listClaims({});
      setClaims(data.claims || []);
    } catch (err) {
      setError(err.message);
      addToast({ title: 'Failed to load claims', description: err.message, variant: 'error' });
    } finally {
      setLoading(false);
    }
  }, [addToast]);

  const loadProviders = useCallback(async () => {
    setLoading(true);
    try {
      const data = await insurance.listProviders();
      setProviders(data.providers || []);
    } catch (err) {
      setError(err.message);
      addToast({ title: 'Failed to load providers', description: err.message, variant: 'error' });
    } finally {
      setLoading(false);
    }
  }, [addToast]);

  useEffect(() => {
    if (activeTab === 'policies') loadPolicies();
    else if (activeTab === 'claims') loadClaims();
    else if (activeTab === 'providers') loadProviders();
  }, [activeTab, loadPolicies, loadClaims, loadProviders]);

  const handleClaimAction = async (claimId, action) => {
    try {
      if (action === 'approve') {
        await insurance.approveClaim(claimId);
      } else if (action === 'reject') {
        const reason = prompt('Enter rejection reason (optional):');
        await insurance.rejectClaim(claimId, reason || '');
      }
      loadClaims();
      const statusLabel = action === 'approve' ? 'approved' : 'rejected';
      addToast({ title: 'Claim updated', description: `Claim ${statusLabel} successfully.`, variant: 'success' });
    } catch (err) {
      addToast({ title: `Failed to ${action} claim`, description: err.message, variant: 'error' });
    }
  };

  const handleProviderAction = async (providerId, status) => {
    try {
      await insurance.approveProvider(providerId, status);
      loadProviders();
      addToast({ title: 'Provider updated', description: `Provider ${status}.`, variant: 'success' });
    } catch (err) {
      addToast({ title: 'Failed to update provider', description: err.message, variant: 'error' });
    }
  };

  return (
    <div className="space-y-6">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-bold text-white">Insurance</h1>
          <p className="text-gray-400">
            {selectedGuild?.name || 'Select a server'}
          </p>
        </div>
        {activeTab === 'policies' && (
          <Button variant="teal" onClick={() => setShowCreateModal(true)}>
            + Create Policy
          </Button>
        )}
      </div>

      {/* Tabs */}
      <div className="flex space-x-1 bg-gray-800 rounded-lg p-1">
        {['policies', 'claims', 'providers'].map((tab) => (
          <button
            key={tab}
            onClick={() => setActiveTab(tab)}
            className={`flex-1 px-4 py-2 rounded-md text-sm font-medium transition-colors ${
              activeTab === tab
                ? 'bg-teal-600 text-white'
                : 'text-gray-400 hover:text-white hover:bg-gray-700'
            }`}
          >
            {tab.charAt(0).toUpperCase() + tab.slice(1)}
          </button>
        ))}
      </div>

      {error && (
        <div className="bg-red-500/20 border border-red-500 rounded-lg p-4 text-red-200">
          {error}
        </div>
      )}

      {/* Content */}
      {loading ? (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-gray-400">Loading...</p>
          </CardContent>
        </Card>
      ) : (
        <>
          {activeTab === 'policies' && (
            <PoliciesList 
              policies={policies} 
              onRefresh={loadPolicies}
            />
          )}
          {activeTab === 'claims' && (
            <ClaimsList 
              claims={claims} 
              onAction={handleClaimAction}
              onRefresh={loadClaims}
            />
          )}
          {activeTab === 'providers' && (
            <ProvidersList 
              providers={providers}
              onAction={handleProviderAction}
              onRefresh={loadProviders}
            />
          )}
        </>
      )}

      {/* Create Modal */}
      <Modal isOpen={showCreateModal} onClose={() => setShowCreateModal(false)}>
        <CreatePolicyForm
          guildId={selectedGuildId}
          onSuccess={() => {
            setShowCreateModal(false);
            loadPolicies();
            addToast({ title: 'Policy created', description: 'Insurance policy created successfully.', variant: 'success' });
          }}
          onCancel={() => setShowCreateModal(false)}
        />
      </Modal>
    </div>
  );
}

function PoliciesList({ policies, onRefresh }) {
  if (policies.length === 0) {
    return (
      <Card>
        <CardContent className="text-center py-12">
          <p className="text-gray-400">No policies found</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {policies.map((policy) => (
        <Card key={policy.policy_id} className="border-teal-600/30">
          <CardContent>
            <div className="flex items-start justify-between">
              <div>
                <div className="flex items-center space-x-3 mb-2">
                  <span className="text-xl">🛡️</span>
                  <span className="text-lg font-semibold text-white">
                    {policy.name}
                  </span>
                  <Badge variant={policy.active ? 'success' : 'error'}>
                    {policy.active ? 'Active' : 'Inactive'}
                  </Badge>
                </div>
                <p className="text-gray-400 text-sm mb-3">{policy.description}</p>
                <div className="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
                  <div>
                    <span className="text-gray-400">Premium:</span>
                    <span className="text-white ml-2">
                      {policy.cost_amount}x {policy.cost_type === 'xanax' ? 'Xanax' : 'DVD'}
                    </span>
                  </div>
                  <div>
                    <span className="text-gray-400">Coverage:</span>
                    <span className="text-white ml-2">
                      {COVERAGE_LABELS[policy.coverage_type] || policy.coverage_type}
                    </span>
                  </div>
                  <div>
                    <span className="text-gray-400">Duration:</span>
                    <span className="text-white ml-2">{policy.duration_hours}h</span>
                  </div>
                  <div>
                    <span className="text-gray-400">Payout:</span>
                    <span className="text-white ml-2">{policy.payout_description}</span>
                  </div>
                </div>
              </div>
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}

function ClaimsList({ claims, onAction, onRefresh }) {
  const pendingClaims = claims.filter(c => c.status === 'pending');
  const resolvedClaims = claims.filter(c => c.status !== 'pending');

  return (
    <div className="space-y-6">
      {pendingClaims.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center space-x-2">
              <span>⚠️</span>
              <span>Pending Claims ({pendingClaims.length})</span>
            </CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {pendingClaims.map((claim) => (
                <ClaimCard key={claim.claim_id} claim={claim} onAction={onAction} />
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {resolvedClaims.length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>Resolved Claims</CardTitle>
          </CardHeader>
          <CardContent>
            <div className="space-y-4">
              {resolvedClaims.map((claim) => (
                <ClaimCard key={claim.claim_id} claim={claim} onAction={onAction} />
              ))}
            </div>
          </CardContent>
        </Card>
      )}

      {claims.length === 0 && (
        <Card>
          <CardContent className="text-center py-12">
            <p className="text-gray-400">No claims found</p>
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function ClaimCard({ claim, onAction }) {
  return (
    <div className="bg-gray-700/50 rounded-lg p-4 border border-gray-600">
      <div className="flex items-start justify-between">
        <div>
          <div className="flex items-center space-x-3 mb-2">
            <span className="font-medium text-white">Claim #{claim.claim_id}</span>
            <Badge status={claim.status}>
              {CLAIM_STATUS_LABELS[claim.status] || claim.status}
            </Badge>
          </div>
          <div className="grid grid-cols-2 gap-4 text-sm">
            <div>
              <span className="text-gray-400">Type:</span>
              <span className="text-white ml-2">{claim.claim_type}</span>
            </div>
            <div>
              <span className="text-gray-400">Payout:</span>
              <span className="text-white ml-2">{claim.payout_amount}x Xanax</span>
            </div>
            <div>
              <span className="text-gray-400">User:</span>
              <span className="text-white ml-2">
                <code className="text-xs bg-gray-700 px-1 rounded">
                  {claim.user_discord_id}
                </code>
              </span>
            </div>
          </div>
        </div>
        
        {claim.status === 'pending' && (
          <div className="flex space-x-2">
            <Button
              size="sm"
              variant="success"
              onClick={() => onAction(claim.claim_id, 'approve')}
            >
              Approve
            </Button>
            <Button
              size="sm"
              variant="danger"
              onClick={() => onAction(claim.claim_id, 'reject')}
            >
              Reject
            </Button>
          </div>
        )}
      </div>
    </div>
  );
}

function ProvidersList({ providers, onAction, onRefresh }) {
  if (providers.length === 0) {
    return (
      <Card>
        <CardContent className="text-center py-12">
          <p className="text-gray-400">No insurance providers found</p>
        </CardContent>
      </Card>
    );
  }

  return (
    <div className="space-y-4">
      {providers.map((provider) => (
        <Card key={provider.provider_id}>
          <CardContent>
            <div className="flex items-center justify-between">
              <div>
                <div className="flex items-center space-x-3 mb-2">
                  <span className="font-semibold text-white">
                    {provider.company_name || `Provider #${provider.provider_id}`}
                  </span>
                  <Badge status={provider.approval_status}>
                    {provider.approval_status}
                  </Badge>
                  {provider.verified && <Badge variant="info">Verified</Badge>}
                </div>
                <div className="text-sm text-gray-400">
                  Torn ID: <code className="bg-gray-700 px-1 rounded">{provider.torn_user_id}</code>
                </div>
              </div>
              
              {provider.approval_status === 'pending' && (
                <div className="flex space-x-2">
                  <Button
                    size="sm"
                    variant="success"
                    onClick={() => onAction(provider.provider_id, 'approved')}
                  >
                    Approve
                  </Button>
                  <Button
                    size="sm"
                    variant="danger"
                    onClick={() => onAction(provider.provider_id, 'rejected')}
                  >
                    Reject
                  </Button>
                </div>
              )}
              
              {provider.approval_status === 'approved' && (
                <Button
                  size="sm"
                  variant="danger"
                  onClick={() => onAction(provider.provider_id, 'disabled')}
                >
                  Disable
                </Button>
              )}
            </div>
          </CardContent>
        </Card>
      ))}
    </div>
  );
}
