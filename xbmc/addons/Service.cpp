/*
 *  Copyright (C) 2005-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */
#include "Service.h"

#include "addons/AddonManager.h"
#include "addons/addoninfo/AddonType.h"
#include "ServiceBroker.h"
#include "interfaces/generic/ScriptInvocationManager.h"
#include "settings/AdvancedSettings.h"
#include "settings/SettingsComponent.h"
#include "utils/StringUtils.h"
#include "utils/log.h"

#include <algorithm>
#include <chrono>
#include <mutex>


namespace ADDON
{

CService::CService(const AddonInfoPtr& addonInfo) : CAddon(addonInfo, AddonType::SERVICE)
{
}

CServiceAddonManager::CServiceAddonManager(CAddonMgr& addonMgr) :
    m_addonMgr(addonMgr)
{
}

CServiceAddonManager::~CServiceAddonManager()
{
  StopDeferredStart();
  m_addonMgr.Events().Unsubscribe(this);
  m_addonMgr.UnloadEvents().Unsubscribe(this);
}

void CServiceAddonManager::OnEvent(const ADDON::AddonEvent& event)
{
  if (typeid(event) == typeid(ADDON::AddonEvents::Enabled))
  {
    Start(event.addonId);
  }
  else if (typeid(event) == typeid(ADDON::AddonEvents::ReInstalled))
  {
    Stop(event.addonId);
    Start(event.addonId);
  }
  else if (typeid(event) == typeid(ADDON::AddonEvents::Disabled) ||
           typeid(event) == typeid(ADDON::AddonEvents::Unload))
  {
    Stop(event.addonId);
  }
}

void CServiceAddonManager::Start()
{
  m_addonMgr.Events().Subscribe(this, [this](const ADDON::AddonEvent& event) { OnEvent(event); });
  m_addonMgr.UnloadEvents().Subscribe(this,
                                      [this](const ADDON::AddonEvent& event) { OnEvent(event); });
  VECADDONS addons;
  if (m_addonMgr.GetAddons(addons, AddonType::SERVICE))
  {
    // LOCAL PATCH (megarushing): services named in advancedsettings <serviceaddons><defer> start
    // <delay> ms later on their own thread, so the home window's rows get the Python lock first
    // (living Shield profile 2026-11-23: Python flat at ~1 core while 2 of 4 cores idle).
    const auto settings = CServiceBroker::GetSettingsComponent()->GetAdvancedSettings();
    const int delayMs = settings->m_serviceDeferMs;
    const auto& deferIds = settings->m_serviceDeferIds;
    VECADDONS later;
    for (const auto& addon : addons)
    {
      if (delayMs > 0 && std::ranges::find(deferIds, addon->ID()) != deferIds.end())
        later.emplace_back(addon);
      else
        Start(addon);
    }
    if (!later.empty())
    {
      CLog::Log(LOGINFO, "CServiceAddonManager: deferring {} services by {} ms", later.size(),
                delayMs);
      StopDeferredStart();
      m_deferStop.Reset();
      m_deferThread = std::thread(
          [this, later, delayMs]
          {
            if (m_deferStop.Wait(std::chrono::milliseconds(delayMs)))
              return; // Kodi is stopping the services
            for (const auto& addon : later)
            {
              if (m_deferStop.Wait(std::chrono::milliseconds(0)))
                return;
              if (!m_addonMgr.IsAddonDisabled(addon->ID()))
                Start(addon);
            }
            CLog::Log(LOGINFO, "CServiceAddonManager: started {} deferred services", later.size());
          });
    }
  }
}

void CServiceAddonManager::StopDeferredStart()
{
  m_deferStop.Set();
  if (m_deferThread.joinable())
    m_deferThread.join();
}

void CServiceAddonManager::Start(const std::string& addonId)
{
  AddonPtr addon;
  if (m_addonMgr.GetAddon(addonId, addon, AddonType::SERVICE, OnlyEnabled::CHOICE_YES))
  {
    Start(addon);
  }
}

void CServiceAddonManager::Start(const AddonPtr& addon)
{
  std::unique_lock lock(m_criticalSection);
  if (m_services.contains(addon->ID()))
  {
    CLog::Log(LOGDEBUG, "CServiceAddonManager: {} already started.", addon->ID());
    return;
  }

  if (StringUtils::EndsWith(addon->LibPath(), ".py"))
  {
    CLog::Log(LOGDEBUG, "CServiceAddonManager: starting {}", addon->ID());
    auto handle = CScriptInvocationManager::GetInstance().ExecuteAsync(addon->LibPath(), addon);
    if (handle == -1)
    {
      CLog::Log(LOGERROR, "CServiceAddonManager: {} failed to start", addon->ID());
      return;
    }
    m_services[addon->ID()] = handle;
  }
}

void CServiceAddonManager::Stop()
{
  StopDeferredStart(); // LOCAL PATCH (megarushing): no deferred start after this
  m_addonMgr.Events().Unsubscribe(this);
  m_addonMgr.UnloadEvents().Unsubscribe(this);
  std::unique_lock lock(m_criticalSection);
  for (const auto& service : m_services)
  {
    Stop(service);
  }
  m_services.clear();
}

void CServiceAddonManager::Stop(const std::string& addonId)
{
  std::unique_lock lock(m_criticalSection);
  auto it = m_services.find(addonId);
  if (it != m_services.end())
  {
    Stop(*it);
    m_services.erase(it);
  }
}

void CServiceAddonManager::Stop(
    const std::map<std::string, int, std::less<>>::value_type& service) const
{
  CLog::Log(LOGDEBUG, "CServiceAddonManager: stopping {}.", service.first);
  CScriptInvocationManager::GetInstance().Stop(service.second);
}
}
