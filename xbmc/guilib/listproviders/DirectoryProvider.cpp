/*
 *  Copyright (C) 2013-2018 Team Kodi
 *  This file is part of Kodi - https://kodi.tv
 *
 *  SPDX-License-Identifier: GPL-2.0-or-later
 *  See LICENSES/README.md for more information.
 */

#include "DirectoryProvider.h"

#include "ContextMenuManager.h"
#include "FileItem.h"
#include "ServiceBroker.h"
#include "addons/AddonEvents.h"
#include "addons/AddonManager.h"
#include "addons/RepositoryUpdater.h"
#include "favourites/FavouritesService.h"
#include "filesystem/Directory.h"
#include "interfaces/AnnouncementManager.h"
#include "interfaces/IAnnouncer.h"
#include "jobs/JobManager.h"
#include "media/MediaType.h"
#include "music/MusicFileItemClassify.h"
#include "music/MusicThumbLoader.h"
#include "pictures/PictureThumbLoader.h"
#include "pvr/PVRManager.h"
#include "pvr/PVRThumbLoader.h"
#include "resources/LocalizeStrings.h"
#include "resources/ResourcesComponent.h"
#include "settings/Settings.h"
#include "settings/SettingsComponent.h"
#include "utils/ExecString.h"
#include "utils/PlayerUtils.h"
#include "utils/SortUtils.h"
#include "utils/StringUtils.h"
#include "utils/URIUtils.h"
#include "utils/Variant.h"
#include "utils/XMLUtils.h"

#include <map>
#include <mutex>
#include "utils/guilib/GUIBuiltinsUtils.h"
#include "utils/guilib/GUIContentUtils.h"
#include "utils/log.h"
#include "video/VideoFileItemClassify.h"
#include "video/VideoInfoTag.h"
#include "video/VideoThumbLoader.h"
#include "video/guilib/VideoGUIUtils.h"
#include "video/guilib/VideoNextUnwatched.h"
#include "video/guilib/VideoPlayActionProcessor.h"
#include "video/guilib/VideoSelectActionProcessor.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <memory>
#include <mutex>
#include <utility>

// LOCK ORDER RULE FOR THIS FILE
//
// Never acquire the GfxContext lock while holding m_section.
//
// The GUI thread takes them in the order GfxContext -> m_section:
// CGUIWindowManager::PreviousWindow() locks the GfxContext (guilib/GUIWindowManager.cpp)
// and holds it across the window close animation, ActivateWindow() and AllocResources(),
// which reaches CGUIBaseContainer::UpdateListProvider() -> Fetch() -> m_section.
//
// So any thread that takes the two in the opposite order deadlocks the whole GUI. Two
// ways into the GfxContext from here, both of which used to run under m_section:
//   * destroying or freeing a CGUIStaticItem - ~CGUIListItem -> CGUIListItemLayout::
//     FreeResources -> CGUITexture::FreeResources -> CGUITextureManager::ReleaseTexture,
//     which locks the GfxContext (guilib/TextureManager.cpp);
//   * evaluating a CGUIInfoLabel/InfoBool - CGUIInfoManager -> GUIINFO::GetWindow ->
//     CGUIWindowManager::GetWindow, which locks the GfxContext.
// Both are reachable off the GUI thread: OnJobComplete() runs on a CJobManager worker and
// OnTimeout() on the CTimer thread. Observed as a permanent GUI hang on stopping playback
// (main thread in PreviousWindow -> Fetch, job worker in OnJobComplete -> ReleaseTexture).
//
// Therefore: evaluate info labels before taking m_section, and hand items that are about
// to die to a local that outlives the lock scope. The info-label members (m_url, m_target,
// m_sortMethod, m_sortOrder, m_limit, m_browse) are written only by the constructors, so
// reading them without the lock is safe.

using namespace XFILE;
using namespace KODI;
using namespace KODI::MESSAGING;
using namespace KODI::UTILS::GUILIB;
using namespace PVR;

class CDirectoryProvider::CSubscriber : public ANNOUNCEMENT::IAnnouncer
{
public:
  explicit CSubscriber(ISubscriberCallback& invalidate) : m_callback(invalidate)
  {
    CServiceBroker::GetAnnouncementManager()->AddAnnouncer(
        this, ANNOUNCEMENT::VideoLibrary | ANNOUNCEMENT::AudioLibrary | ANNOUNCEMENT::Player |
                  ANNOUNCEMENT::GUI);
  }
  ~CSubscriber() override { CServiceBroker::GetAnnouncementManager()->RemoveAnnouncer(this); }

  virtual bool IsReadyToUse() const { return true; }

protected:
  bool OnEventPublished(Topic topic = Topic::UNSPECIFIED)
  {
    return m_callback.OnEventPublished(topic);
  }

private:
  CSubscriber() = delete;

  // IAnnouncer implementation
  void Announce(ANNOUNCEMENT::AnnouncementFlag flag,
                const std::string& sender,
                const std::string& message,
                const CVariant& data) override
  {
    if (flag & ANNOUNCEMENT::VideoLibrary && OnEventPublished(Topic::VIDEO_LIBRARY))
      return;

    if (flag & ANNOUNCEMENT::AudioLibrary && OnEventPublished(Topic::AUDIO_LIBRARY))
      return;

    if (flag & ANNOUNCEMENT::Player)
    {
      if (message == "OnPlay" || message == "OnResume" || message == "OnStop")
        OnEventPublished(Topic::PLAYER);
    }
    else
    {
      // if we're in a database transaction, don't bother doing anything just yet
      if (data.isMember("transaction") && data["transaction"].asBoolean())
        return;

      // if there was a database update, we set the update state
      // to PENDING to fire off a new job in the next update
      if (message == "OnScanFinished" || message == "OnCleanFinished" || message == "OnUpdate" ||
          message == "OnRemove" || message == "OnRefresh")
        OnEventPublished();
    }
  }

  CCriticalSection m_critSection;
  ISubscriberCallback& m_callback;
};

namespace
{
class CAddonsSubscriber : public CDirectoryProvider::CSubscriber
{
public:
  explicit CAddonsSubscriber(ISubscriberCallback& invalidate)
    : CDirectoryProvider::CSubscriber(invalidate)
  {
    CServiceBroker::GetAddonMgr().Events().Subscribe(
        this,
        [this](const ADDON::AddonEvent& event)
        {
          if (typeid(event) == typeid(ADDON::AddonEvents::Enabled) ||
              typeid(event) == typeid(ADDON::AddonEvents::Disabled) ||
              typeid(event) == typeid(ADDON::AddonEvents::ReInstalled) ||
              typeid(event) == typeid(ADDON::AddonEvents::UnInstalled) ||
              typeid(event) == typeid(ADDON::AddonEvents::MetadataChanged) ||
              typeid(event) == typeid(ADDON::AddonEvents::AutoUpdateStateChanged))
          {
            OnEventPublished();
          }
        });

    CServiceBroker::GetRepositoryUpdater().Events().Subscribe(
        this, [this](const ADDON::CRepositoryUpdater::RepositoryUpdated& /*event*/)
        { OnEventPublished(); });
  }
  ~CAddonsSubscriber() override
  {
    CServiceBroker::GetRepositoryUpdater().Events().Unsubscribe(this);
    CServiceBroker::GetAddonMgr().Events().Unsubscribe(this);
  }
};

class CPVRSubscriber : public CDirectoryProvider::CSubscriber
{
public:
  explicit CPVRSubscriber(ISubscriberCallback& invalidate)
    : CDirectoryProvider::CSubscriber(invalidate)
  {
    CServiceBroker::GetPVRManager().Events().Subscribe(
        this,
        [this](const PVR::PVREvent& event)
        {
          if (event == PVR::PVREvent::ManagerStarted)
            m_pvrStarted.test_and_set();
          else if (event == PVR::PVREvent::ManagerStarting ||
                   event == PVR::PVREvent::ManagerStopping ||
                   event == PVR::PVREvent::ManagerStopped)
            m_pvrStarted.clear();

          if (!m_pvrStarted.test())
            return;

          using enum PVR::PVREvent;
          if (event == ManagerStarted || event == ManagerStopped ||
              event == RecordingsInvalidated || event == TimersInvalidated ||
              event == ChannelGroupsInvalidated || event == SavedSearchesInvalidated ||
              event == ClientsInvalidated || event == ClientsPrioritiesInvalidated)
          {
            OnEventPublished();
          }
        });

    if (CServiceBroker::GetPVRManager().IsStarted())
      m_pvrStarted.test_and_set();
  }
  ~CPVRSubscriber() override { CServiceBroker::GetPVRManager().Events().Unsubscribe(this); }

  bool IsReadyToUse() const override { return m_pvrStarted.test(); }

private:
  std::atomic_flag m_pvrStarted{};
};

class CFavouritesSubscriber : public CDirectoryProvider::CSubscriber
{
public:
  explicit CFavouritesSubscriber(ISubscriberCallback& invalidate)
    : CDirectoryProvider::CSubscriber(invalidate)
  {
    CServiceBroker::GetFavouritesService().Events().Subscribe(
        this,
        [this](const CFavouritesService::FavouritesUpdated& /*event*/) { OnEventPublished(); });
  }
  ~CFavouritesSubscriber() override
  {
    CServiceBroker::GetFavouritesService().Events().Unsubscribe(this);
  }
};

std::unique_ptr<CDirectoryProvider::CSubscriber> GetSubscriber(const std::string& url,
                                                               ISubscriberCallback& invalidate)
{
  if (URIUtils::IsProtocol(url, "addons"))
    return std::make_unique<CAddonsSubscriber>(invalidate);
  else if (URIUtils::IsProtocol(url, "pvr"))
    return std::make_unique<CPVRSubscriber>(invalidate);
  else if (URIUtils::IsProtocol(url, "favourites"))
    return std::make_unique<CFavouritesSubscriber>(invalidate);
  else
    return std::make_unique<CDirectoryProvider::CSubscriber>(invalidate);
}

class CDirectoryJob : public CJob
{
public:
  CDirectoryJob(const std::string& url,
                const std::string& target,
                SortDescription sort,
                int limit,
                CDirectoryProvider::BrowseMode browse,
                int parentID)
    : m_url(url),
      m_targetKey(target),
      m_target(target),
      m_sort(sort),
      m_limit(limit),
      m_browse(browse),
      m_parentID(parentID),
      m_queued(std::chrono::steady_clock::now()) // LOCAL PATCH (megarushing): WidgetTiming
  {
  }
  ~CDirectoryJob() override = default;

  const char* GetType() const override { return "directory"; }
  bool Equals(const CJob* job) const override
  {
    if (strcmp(job->GetType(), GetType()) == 0)
    {
      const auto* dirJob = dynamic_cast<const CDirectoryJob*>(job);
      if (dirJob && dirJob->m_url == m_url && dirJob->m_targetKey == m_targetKey &&
          dirJob->m_sort.sortBy == m_sort.sortBy && dirJob->m_sort.sortOrder == m_sort.sortOrder &&
          dirJob->m_sort.sortAttributes == m_sort.sortAttributes &&
          dirJob->m_sort.limitStart == m_sort.limitStart &&
          dirJob->m_sort.limitEnd == m_sort.limitEnd && dirJob->m_limit == m_limit &&
          dirJob->m_browse == m_browse && dirJob->m_parentID == m_parentID)
        return true;
    }
    return false;
  }

  bool DoWork() override
  {
    // LOCAL PATCH (megarushing): WidgetTiming log = queue wait, directory fetch, thumb load per widget.
    using clock = std::chrono::steady_clock;
    auto ms = [](clock::time_point a, clock::time_point b) {
      return static_cast<long>(std::chrono::duration_cast<std::chrono::milliseconds>(b - a).count());
    };
    const auto started = clock::now();
    CFileItemList items;
    const bool fetched = CDirectory::GetDirectory(m_url, items, "", DIR_FLAG_DEFAULTS);
    const auto gotDir = clock::now();
    clock::time_point thumbsDone = gotDir;
    if (fetched)
    {
      // sort the items if necessary
      if (m_sort.sortBy != SortBy::NONE)
        items.Sort(m_sort);

      // limit must not exceed the number of items
      int limit = (m_limit == 0) ? items.Size() : std::min(static_cast<int>(m_limit), items.Size());
      if (limit < items.Size())
        m_items.reserve(limit + 1);
      else
        m_items.reserve(limit);

      for (int i = 0; i < limit; i++)
      {
        auto item{std::make_shared<CGUIStaticItem>(*items[i])};
        if (item->HasProperty("node.visible"))
          item->SetVisibleCondition(item->GetProperty("node.visible").asString(), m_parentID);

        GetThumbLoader(item)->LoadItem(item.get());

        m_items.emplace_back(std::move(item));
      }
      thumbsDone = clock::now();

      if (items.HasProperty("node.target"))
        m_target = items.GetProperty("node.target").asString();

      if ((m_browse == CDirectoryProvider::BrowseMode::ALWAYS && !items.IsEmpty()) ||
          (m_browse == CDirectoryProvider::BrowseMode::AUTO && limit < items.Size()))
      {
        // Add a special item to the end of the list, which can be used to open the
        // full listing containing all items in the given target window.
        if (!m_target.empty())
        {
          CFileItem item(m_url, true);
          item.SetLabel(
              CServiceBroker::GetResourcesComponent().GetLocalizeStrings().Get(22082)); // More...
          item.SetArt("icon", "DefaultFolder.png");
          item.SetProperty("node.target", m_target);
          item.SetProperty("node.type", "target_folder"); // make item identifiable, e.g. by skins

          m_items.emplace_back(std::make_shared<CGUIStaticItem>(item));
        }
        else
          CLog::LogF(LOGWARNING, "Cannot add 'More...' item to list. No target window given.");
      }
    }
    CLog::Log(LOGINFO, "WidgetTiming: wait {} fetch {} thumbs {} items {}{} {}", ms(m_queued, started),
              ms(started, gotDir), ms(gotDir, thumbsDone), items.Size(), fetched ? "" : " FAILED",
              m_url.substr(0, 180));
    return true;
  }

  std::shared_ptr<CThumbLoader> GetThumbLoader(const std::shared_ptr<CGUIStaticItem>& item)
  {
    using enum InfoTagType;

    if (VIDEO::IsVideo(*item))
    {
      InitThumbLoader<CVideoThumbLoader>(VIDEO);
      return m_thumbloaders[InfoTagType::VIDEO];
    }
    if (MUSIC::IsAudio(*item))
    {
      InitThumbLoader<CMusicThumbLoader>(AUDIO);
      return m_thumbloaders[InfoTagType::AUDIO];
    }
    if (item->IsPicture())
    {
      InitThumbLoader<CPictureThumbLoader>(PICTURE);
      return m_thumbloaders[InfoTagType::PICTURE];
    }
    if (item->IsPVRChannelGroup())
    {
      InitThumbLoader<CPVRThumbLoader>(PVR);
      return m_thumbloaders[PVR];
    }
    InitThumbLoader<CProgramThumbLoader>(PROGRAM);
    return m_thumbloaders[PROGRAM];
  }

  template<class CThumbLoaderClass>
  void InitThumbLoader(InfoTagType type)
  {
    if (!m_thumbloaders.contains(type))
    {
      auto thumbLoader{std::make_shared<CThumbLoaderClass>()};
      thumbLoader->OnLoaderStart();
      m_thumbloaders.insert({type, std::move(thumbLoader)});
    }
  }

  const std::vector<CGUIStaticItemPtr>& GetItems() const { return m_items; }
  const std::string& GetTarget() const { return m_target; }
  std::vector<InfoTagType> GetItemTypes(std::vector<InfoTagType>& itemTypes) const
  {
    itemTypes.clear();
    for (const auto& [type, _] : m_thumbloaders)
      itemTypes.emplace_back(type);
    return itemTypes;
  }

private:
  std::string m_url;
  std::string m_targetKey;
  std::string m_target;
  SortDescription m_sort;
  unsigned int m_limit{10};
  CDirectoryProvider::BrowseMode m_browse{CDirectoryProvider::BrowseMode::AUTO};
  int m_parentID;
  std::chrono::steady_clock::time_point m_queued;
  std::vector<CGUIStaticItemPtr> m_items;
  std::map<InfoTagType, std::shared_ptr<CThumbLoader>> m_thumbloaders;
};
} // unnamed namespace

CDirectoryProvider::CDirectoryProvider(const TiXmlElement* element, int parentID)
  : IListProvider(parentID),
    m_nextJobTimer(this)
{
  assert(element);
  if (!element->NoChildren())
  {
    const char* target = element->Attribute("target");
    if (target)
      m_target.SetLabel(target, "", parentID);

    const char* sortMethod = element->Attribute("sortby");
    if (sortMethod)
      m_sortMethod.SetLabel(sortMethod, "", parentID);

    const char* sortOrder = element->Attribute("sortorder");
    if (sortOrder)
      m_sortOrder.SetLabel(sortOrder, "", parentID);

    const char* limit = element->Attribute("limit");
    if (limit)
      m_limit.SetLabel(limit, "", parentID);

    const char* browse = element->Attribute("browse");
    if (browse)
      m_browse.SetLabel(browse, "", parentID);

    m_url.SetLabel(element->FirstChild()->ValueStr(), "", parentID);
  }
}

CDirectoryProvider::CDirectoryProvider(const CDirectoryProvider& other)
  : IListProvider(other),
    m_updateState(UpdateState::INVALIDATED),
    m_nextJobTimer(this),
    m_url(other.m_url),
    m_target(other.m_target),
    m_sortMethod(other.m_sortMethod),
    m_sortOrder(other.m_sortOrder),
    m_limit(other.m_limit),
    m_browse(other.m_browse),
    m_currentUrl(other.m_currentUrl),
    m_currentTarget(other.m_currentTarget),
    m_currentSort(other.m_currentSort),
    m_currentLimit(other.m_currentLimit),
    m_currentBrowse(other.m_currentBrowse)
{
}

CDirectoryProvider::~CDirectoryProvider()
{
  Reset();
}

std::unique_ptr<IListProvider> CDirectoryProvider::Clone()
{
  return std::make_unique<CDirectoryProvider>(*this);
}

// LOCAL PATCH (megarushing): settle delay - see NoteWindowInit() in the header. Measured
// 2026-11-20 (living Shield): passing tab 1101 on the way to True Crime left 6 of its plugin rows
// running (12-28 s each); True Crime's last row then filled after 44 s instead of ~20 s.
namespace
{
// Counted from the end of the window load (CGUIWindow notes it again after AllocResources: an
// Arctic Fuse hub takes ~650 ms to load). Its tab bar switches windows ~1.35 s apart even on fast
// key presses, so a tab passed on the way stays open ~0.7 s after its load.
constexpr std::chrono::milliseconds WIDGET_SETTLE_TIME{1000};

std::mutex& SettleMutex()
{
  static std::mutex mutex;
  return mutex;
}

std::map<int, std::chrono::steady_clock::time_point>& WindowInitTimes()
{
  static std::map<int, std::chrono::steady_clock::time_point> times;
  return times;
}

// Time left before a list of this window may start a plugin fetch; zero when it may start now.
std::chrono::milliseconds SettleTimeLeft(int windowID)
{
  std::lock_guard lock(SettleMutex());
  const auto it = WindowInitTimes().find(windowID);
  if (it == WindowInitTimes().end())
    return std::chrono::milliseconds{0};
  const auto openFor = std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now() - it->second);
  return openFor >= WIDGET_SETTLE_TIME ? std::chrono::milliseconds{0}
                                       : WIDGET_SETTLE_TIME - openFor;
}
} // namespace

void CDirectoryProvider::NoteWindowInit(int windowID)
{
  std::lock_guard lock(SettleMutex());
  WindowInitTimes()[windowID] = std::chrono::steady_clock::now();
}

void CDirectoryProvider::StartDirectoryJob(const char* why)
{
  // Evaluated before m_section is taken - see the lock order rule at the top of this file.
  // Callers must not hold m_section: this runs on the job worker and on the timer thread.
  const std::string target{m_target.GetLabel(GetParentId(), false)};

  std::unique_lock lock(m_section);
  m_jobPending = false;
  m_lastJobStartedAt = std::chrono::system_clock::now();
  m_nextJobTimer.Stop();

  CLog::Log(LOGDEBUG, "CDirectoryProvider[{}]: refreshing...", m_currentUrl);
  // LOCAL PATCH (megarushing): Kodi starts LOW jobs only while fewer than 3 jobs of any kind run,
  // so local lists (addons://, library) waited behind poster decodes and slow plugin fetches.
  // Plugin fetches stay LOW; everything else is NORMAL.
  const CJob::PRIORITY priority =
      StringUtils::StartsWith(m_currentUrl, "plugin://") ? CJob::PRIORITY_LOW : CJob::PRIORITY_NORMAL;
  m_jobID = CServiceBroker::GetJobManager()->AddJob(
      new CDirectoryJob(m_currentUrl, target, m_currentSort, m_currentLimit, m_currentBrowse,
                        GetParentId()),
      this, priority);
  // LOCAL PATCH (megarushing): WidgetDebug - who queued this fetch, for which window.
  CLog::Log(LOGINFO, "WidgetDebug: start {} win {} job {} {}", why, GetParentId(), m_jobID,
            m_currentUrl.substr(0, 120));
}

bool CDirectoryProvider::Update(bool forceRefresh)
{
  // we never need to force refresh here
  bool changed = false;
  bool fireJob = false;

  // update the URL & limit and fire off a new job if needed
  // LOCAL PATCH (megarushing): WidgetDebug - name the change that fires a job.
  std::string startWhy;
  if (UpdateURL()) { fireJob = true; startWhy += "url,"; }
  if (UpdateSort()) { fireJob = true; startWhy += "sort,"; }
  if (UpdateLimit()) { fireJob = true; startWhy += "limit,"; }
  if (UpdateBrowse()) { fireJob = true; startWhy += "browse,"; }
  fireJob &= !m_currentUrl.empty();

  bool startJob = false;
  std::chrono::milliseconds settleDelay{0}; // LOCAL PATCH (megarushing): settle delay
  // Snapshot taken under m_section; visibility is evaluated after it is released, because
  // InfoBool::Get() reaches the GfxContext - see the lock order rule at the top of this file.
  std::vector<CGUIStaticItemPtr> items;

  {
    std::unique_lock lock(m_section);
    if (m_updateState == UpdateState::INVALIDATED)
    {
      fireJob = true;
      startWhy += "invalidated,";
    }
    else if (m_updateState == UpdateState::DONE)
      changed = true;

    m_updateState = UpdateState::OK;

    if (fireJob)
    {
      if (m_jobID)
      {
        // Ignore update request for now.
        // We will start another update job once the currently running has finished.
        if (!m_jobPending)
          CLog::Log(LOGINFO, "WidgetDebug: pending {}while job {} win {} {}", startWhy, m_jobID,
                    GetParentId(), m_currentUrl.substr(0, 120));
        m_jobPending = true;
        changed = false;
      }
      else
      {
        if (m_jobPending)
        {
          // Ignore the update request.
          // We already have scheduled another update job.
          changed = false;
        }
        else
        {
          // Start a new update job, once m_section is released.
          startJob = true;
          // LOCAL PATCH (megarushing): settle delay - a plugin list of a window opened less than
          // WIDGET_SETTLE_TIME ago waits on m_nextJobTimer (OnTimeout starts it while still
          // pending). Leaving the window first clears the pending flag and invalidates the list
          // in CancelPendingUpdate(), so it is fetched on the next visit and never left empty.
          if (StringUtils::StartsWith(m_currentUrl, "plugin://"))
            settleDelay = SettleTimeLeft(GetParentId());
          if (settleDelay.count() > 0)
          {
            startJob = false;
            m_jobPending = true;
            changed = false;
          }
        }
      }
    }

    if (!changed)
      items = m_items;
  }

  if (startJob)
    StartDirectoryJob(startWhy.c_str());
  else if (settleDelay.count() > 0)
  {
    // LOCAL PATCH (megarushing): settle delay; outside m_section like OnJobComplete's timer start.
    CLog::Log(LOGINFO, "WidgetDebug: settle {}ms {}win {} {}", settleDelay.count(), startWhy,
              GetParentId(), m_currentUrl.substr(0, 120));
    m_nextJobTimer.Start(settleDelay);
  }

  for (const auto& i : items)
    changed |= i->UpdateVisibility(GetParentId());

  return changed; //! @todo Also returned changed if properties are changed (if so, need to update scroll to letter).
}

void CDirectoryProvider::Fetch(std::vector<std::shared_ptr<CGUIListItem>>& items)
{
  std::vector<std::shared_ptr<CGUIListItem>> fetched;
  {
    std::unique_lock lock(m_section);
    for (const auto& i : m_items)
    {
      if (i->IsVisible())
        fetched.push_back(i);
    }
  }
  // swap rather than clear-and-fill under the lock: the caller's previous items may hold the
  // last reference, and destroying them reaches the texture manager - see the lock order rule
  // at the top of this file. The old contents die with `fetched`, after m_section is released.
  items.swap(fetched);
}

void CDirectoryProvider::Reset()
{
  // Destroyed after m_section is released - see the lock order rule at the top of this file.
  std::vector<CGUIStaticItemPtr> oldItems;
  {
    std::unique_lock lock(m_section);
    if (m_jobID)
      CServiceBroker::GetJobManager()->CancelJob(m_jobID);
    m_jobID = 0;
    m_jobPending = false;
    m_lastJobStartedAt = {};
    m_nextJobTimer.Stop();
    oldItems.swap(m_items);
    m_currentTarget.clear();
    m_currentUrl.clear();
    m_itemTypes.clear();
    m_currentSort.sortBy = SortBy::NONE;
    m_currentSort.sortOrder = SortOrder::ASCENDING;
    m_currentLimit = 0;
    m_currentBrowse = BrowseMode::AUTO;
    m_updateState = UpdateState::OK;
  }

  oldItems.clear(); // GUI teardown, outside m_section

  {
    std::unique_lock subscriptionLock(m_subscriptionSection);
    m_subscriber.reset();
  }
}

// LOCAL PATCH (megarushing): see IListProvider. Leaving an Arctic Fuse hub tab left its widget
// fetches (TMDb Helper Python, 1-2 s each) running for minutes: measured 2026-10-18, the Add-ons
// hub's addons:// rows waited behind them, so its icons appeared very late.
void CDirectoryProvider::CancelPendingUpdate()
{
  std::unique_lock lock(m_section);
  // LOCAL PATCH (megarushing): WidgetDebug - log every cancel attempt, not only the hits.
  CLog::Log(LOGINFO, "WidgetDebug: cancel win {} job {} pending {} state {} {}", GetParentId(),
            m_jobID, m_jobPending, static_cast<int>(m_updateState), m_currentUrl.substr(0, 120));
  // LOCAL PATCH (megarushing): a fetch that was only waiting (settle delay, or the 1 s rate limit
  // after a finished job) has no job to cancel; mark the list for a fetch on the next visit.
  if (m_jobPending && !m_jobID)
    m_updateState = UpdateState::INVALIDATED;
  m_jobPending = false;
  m_nextJobTimer.Stop();
  // A fetch that already runs is left to finish (its result is still wanted); a queued one is
  // dropped and fetched again when the list is processed next (m_updateState = INVALIDATED).
  if (m_jobID && CServiceBroker::GetJobManager()->CancelJobIfQueued(m_jobID))
  {
    m_jobID = 0;
    m_updateState = UpdateState::INVALIDATED;
    CLog::Log(LOGINFO, "WidgetTiming: cancelled {}", m_currentUrl.substr(0, 120));
  }
}

void CDirectoryProvider::FreeResources(bool immediately)
{
  std::vector<CGUIStaticItemPtr> items;
  {
    std::unique_lock lock(m_section);
    items = m_items;
  }
  // FreeMemory() reaches CGUITextureManager::ReleaseTexture and therefore the GfxContext -
  // see the lock order rule at the top of this file.
  for (const auto& item : items)
    item->FreeMemory(immediately);
}

void CDirectoryProvider::OnJobComplete(unsigned int jobID, bool success, CJob* job)
{
  // This runs on a CJobManager worker. Neither the destruction of the previous items nor the
  // follow-up StartDirectoryJob() may happen under m_section, because both reach the
  // GfxContext - see the lock order rule at the top of this file. Deferring them here is what
  // stops this callback deadlocking against the GUI thread in CGUIWindowManager::PreviousWindow.
  std::vector<CGUIStaticItemPtr> oldItems;
  bool startJobNow{false};
  std::chrono::milliseconds startJobDelay{0};

  {
    std::unique_lock lock(m_section);
    if (success)
    {
      oldItems.swap(m_items); // hand the outgoing items to the local, destroyed after unlock

      if (job->GetPendingCallbackCount() > 1)
      {
        // Deep copy items since other callbacks will also receive this job's results,
        // and each container needs independent visibility state and layout
        const auto& sourceItems = static_cast<CDirectoryJob*>(job)->GetItems();
        m_items.reserve(sourceItems.size());
        for (const auto& item : sourceItems)
          m_items.emplace_back(std::make_shared<CGUIStaticItem>(*item));
      }
      else
      {
        m_items = static_cast<CDirectoryJob*>(job)->GetItems();
      }

      m_currentTarget = static_cast<CDirectoryJob*>(job)->GetTarget();
      static_cast<CDirectoryJob*>(job)->GetItemTypes(m_itemTypes);
      if (m_updateState == UpdateState::OK)
        m_updateState = UpdateState::DONE;
    }
    m_jobID = 0;

    if (m_jobPending)
    {
      // Handle delayed update request(s).

      using namespace std::chrono_literals;
      static constexpr auto JOB_RATE_LIMIT = 1s;
      const auto now{std::chrono::system_clock::now()};
      const auto nextJobAllowedAt{m_lastJobStartedAt + JOB_RATE_LIMIT};

      if (now >= nextJobAllowedAt)
        // Finished job ended after job schedule timeslice was over. Start a new update job now.
        startJobNow = true;
      else
        // Finished job ended before the timeslice was over. Start a new update job delayed.
        startJobDelay =
            std::chrono::duration_cast<std::chrono::milliseconds>(nextJobAllowedAt - now);
    }
  }

  // The provider state above is fully published before either of these runs.
  oldItems.clear(); // GUI teardown, outside m_section

  if (startJobNow)
    StartDirectoryJob("jobcomplete-pending");
  else if (startJobDelay.count() > 0)
    m_nextJobTimer.Start(startJobDelay);
}

void CDirectoryProvider::OnTimeout()
{
  bool startJob{false};
  std::chrono::milliseconds settleLeft{0};
  {
    std::unique_lock lock(m_section);
    startJob = m_jobPending;
    // LOCAL PATCH (megarushing): settle delay - the window may have been noted again after its
    // load, so re-check and wait the rest instead of starting early.
    if (startJob && !m_jobID && StringUtils::StartsWith(m_currentUrl, "plugin://"))
      settleLeft = SettleTimeLeft(GetParentId());
  }
  if (settleLeft.count() > 0)
  {
    // Inside its own callback the timer is still running, so Start() would refuse (and the list
    // would never be fetched); RestartAsync() is CTimer's "restarted during callback" path.
    m_nextJobTimer.RestartAsync(settleLeft);
    return;
  }

  // Outside m_section - this runs on the timer thread and StartDirectoryJob() reaches the
  // GfxContext; see the lock order rule at the top of this file.
  if (startJob)
    StartDirectoryJob("timer");
}

bool CDirectoryProvider::OnEventPublished(Topic topic /*= Topic::UNSPECIFIED*/)
{
  std::unique_lock lock(m_section);
  switch (topic)
  {
    case Topic::UNSPECIFIED:
      // Always invalidate.
      break;
    case Topic::PLAYER:
      // We don't need to do anything if there is no matching sort method.
      if (m_currentSort.sortBy != SortBy::NONE && m_currentSort.sortBy != SortBy::LAST_PLAYED &&
          m_currentSort.sortBy != SortBy::DATE_ADDED && m_currentSort.sortBy != SortBy::PLAYCOUNT &&
          m_currentSort.sortBy != SortBy::LAST_USED)
        return false;
      break;
    case Topic::VIDEO_LIBRARY:
      // We don't need to do anything if there are no fitting items.
      return std::ranges::find(m_itemTypes, InfoTagType::VIDEO) == m_itemTypes.cend();
    case Topic::AUDIO_LIBRARY:
      // We don't need to do anything if there are no fitting items.
      return std::ranges::find(m_itemTypes, InfoTagType::AUDIO) == m_itemTypes.cend();
  }
  m_updateState = UpdateState::INVALIDATED;
  return true;
}

std::string CDirectoryProvider::GetTarget(const CFileItem& item) const
{
  std::string target = item.GetProperty("node.target").asString();

  if (target.empty())
  {
    std::unique_lock lock(m_section);
    target = m_currentTarget;
  }
  // Evaluated outside m_section - see the lock order rule at the top of this file.
  if (target.empty())
    target = m_target.GetLabel(GetParentId(), false);

  return target;
}

bool CDirectoryProvider::OnClick(const std::shared_ptr<CGUIListItem>& item)
{
  std::shared_ptr<CFileItem> targetItem{std::static_pointer_cast<CFileItem>(item)};

  if (targetItem->IsFavourite())
  {
    targetItem = CServiceBroker::GetFavouritesService().ResolveFavourite(*targetItem);
    if (!targetItem)
      return false;
  }

  const CExecString exec{*targetItem, GetTarget(*targetItem)};
  const bool isPlayMedia{exec.GetFunction() == "playmedia"};

  // video select action setting is for files only, except exec func is playmedia...
  if (targetItem->HasVideoInfoTag() && (!targetItem->IsFolder() || isPlayMedia))
  {
    const std::string targetWindow{GetTarget(*targetItem)};
    if (!targetWindow.empty())
      targetItem->SetProperty("targetwindow", targetWindow);

    KODI::VIDEO::GUILIB::CVideoSelectActionProcessor proc{targetItem};
    if (proc.ProcessDefaultAction())
      return true;
  }

  // exec the execute string for the original (!) item
  CFileItem fileItem{*std::static_pointer_cast<CFileItem>(item)};

  if (fileItem.HasProperty("node.target_url"))
    fileItem.SetPath(fileItem.GetProperty("node.target_url").asString());

  return CGUIBuiltinsUtils::ExecuteAction({fileItem, GetTarget(fileItem)}, targetItem);
}

bool CDirectoryProvider::OnPlay(const std::shared_ptr<CGUIListItem>& item)
{
  std::shared_ptr<CFileItem> targetItem{std::static_pointer_cast<CFileItem>(item)};

  if (targetItem->IsFavourite())
  {
    targetItem = CServiceBroker::GetFavouritesService().ResolveFavourite(*targetItem);
    if (!targetItem)
      return false;
  }

  // video play action setting is for files and folders...
  if (targetItem->HasVideoInfoTag() ||
      (targetItem->IsFolder() && VIDEO::UTILS::IsItemPlayable(*targetItem)))
  {
    KODI::VIDEO::GUILIB::CVideoPlayActionProcessor proc{targetItem};
    if (proc.ProcessDefaultAction())
      return true;
  }

  if (CPlayerUtils::IsItemPlayable(*targetItem))
  {
    const CExecString exec{*targetItem, GetTarget(*targetItem)};
    if (exec.GetFunction() == "playmedia")
    {
      // exec as is
      return CGUIBuiltinsUtils::ExecuteAction(exec, targetItem);
    }
    else
    {
      // build a playmedia execute string for given target and exec this
      return CGUIBuiltinsUtils::ExecutePlayMediaAskResume(targetItem);
    }
  }
  return true;
}

bool CDirectoryProvider::OnInfo(const std::shared_ptr<CGUIListItem>& item)
{
  const auto fileItem{std::static_pointer_cast<CFileItem>(item)};
  const auto targetItem{fileItem->IsFavourite()
                            ? CServiceBroker::GetFavouritesService().ResolveFavourite(*fileItem)
                            : fileItem};

  return CGUIContentUtils::ShowInfoForItem(*targetItem);
}

bool CDirectoryProvider::OnContextMenu(const std::shared_ptr<CGUIListItem>& item)
{
  const auto fileItem{std::static_pointer_cast<CFileItem>(item)};
  const std::string target{GetTarget(*fileItem)};
  if (!target.empty())
    fileItem->SetProperty("targetwindow", target);

  return CONTEXTMENU::ShowFor(fileItem, CContextMenuManager::MAIN);
}

bool CDirectoryProvider::IsUpdating() const
{
  {
    std::unique_lock lock(m_section);
    if (m_jobID || m_jobPending || m_updateState == UpdateState::DONE ||
        m_updateState == UpdateState::INVALIDATED)
      return true;
  }

  std::unique_lock subscriptionLock(m_subscriptionSection);
  return m_subscriber && !m_subscriber->IsReadyToUse();
}

// LOCAL PATCH (megarushing): a widget holding one season of one show (e.g. Arctic Fuse's
// episode row under the season tabs) starts at the first unwatched episode after the last
// watched one, following the "select first unwatched TV show season / episode" setting.
int CDirectoryProvider::GetInitialItem(
    const std::vector<std::shared_ptr<CGUIListItem>>& items) const
{
  if (items.size() < 2 ||
      CServiceBroker::GetSettingsComponent()->GetSettings()->GetInt(
          CSettings::SETTING_VIDEOLIBRARY_TVSHOWSSELECTFIRSTUNWATCHEDITEM) == 0)
    return -1;

  std::vector<KODI::VIDEO::GUILIB::NextUnwatchedEntry> entries;
  const CVideoInfoTag* first{nullptr};
  for (int i = 0; i < static_cast<int>(items.size()); ++i)
  {
    if (!items[i]->IsFileItem())
      return -1;
    const auto& fileItem{static_cast<const CFileItem&>(*items[i])};
    if (!fileItem.HasVideoInfoTag())
      return -1;
    const CVideoInfoTag* tag{fileItem.GetVideoInfoTag()};
    if (tag->m_type != MediaTypeEpisode || tag->m_iEpisode < 0)
      return -1;
    if (!first)
      first = tag;
    else if (tag->m_iSeason != first->m_iSeason || tag->m_strShowTitle != first->m_strShowTitle)
      return -1;
    entries.push_back({i, tag->m_iSeason, tag->m_iEpisode, tag->GetPlayCount() > 0, false});
  }
  // A fully watched season starts at its first episode, not at a position left from the
  // previous list.
  return std::max(KODI::VIDEO::GUILIB::GetNextUnwatchedIndex(entries), 0);
}

// LOCAL PATCH (megarushing): a list hidden because it is empty (Arctic Fuse: row group visible only
// with items or while updating) is never processed, so it never sees its content URL change.
// Seen 2026-11-23: Trakt rows fetched before TraktHub.Ready was set failed with "info=..." and
// stayed empty until restart. Only a list that already fetched and holds nothing is checked.
bool CDirectoryProvider::EmptyUrlChanged()
{
  {
    std::unique_lock lock(m_section);
    if (m_currentUrl.empty() || !m_items.empty() || m_jobID || m_jobPending ||
        m_updateState == UpdateState::INVALIDATED)
      return false;
  }
  // Evaluated outside m_section - see the lock order rule at the top of this file.
  const std::string value{m_url.GetLabel(GetParentId(), false)};
  std::unique_lock lock(m_section);
  return value != m_currentUrl;
}

bool CDirectoryProvider::UpdateURL()
{
  // Evaluated before m_section is taken - see the lock order rule at the top of this file.
  const std::string value{m_url.GetLabel(GetParentId(), false)};
  {
    std::unique_lock lock(m_section);
    if (value == m_currentUrl)
      return false;

    m_currentUrl = value;
  }

  std::unique_lock subscriptionLock(m_subscriptionSection);
  m_subscriber = GetSubscriber(value, *this);
  return true;
}

bool CDirectoryProvider::UpdateLimit()
{
  // Evaluated before m_section is taken - see the lock order rule at the top of this file.
  const unsigned int value = m_limit.GetIntValue(GetParentId());

  std::unique_lock lock(m_section);
  if (value == m_currentLimit)
    return false;

  m_currentLimit = value;
  return true;
}

bool CDirectoryProvider::UpdateBrowse()
{
  // Evaluated before m_section is taken - see the lock order rule at the top of this file.
  const std::string stringValue{m_browse.GetLabel(GetParentId(), false)};

  std::unique_lock lock(m_section);
  BrowseMode value{m_currentBrowse};
  if (StringUtils::EqualsNoCase(stringValue, "always"))
    value = BrowseMode::ALWAYS;
  else if (StringUtils::EqualsNoCase(stringValue, "auto"))
    value = BrowseMode::AUTO;
  else if (StringUtils::EqualsNoCase(stringValue, "never"))
    value = BrowseMode::NEVER;

  if (value == m_currentBrowse)
    return false;

  m_currentBrowse = value;
  return true;
}

bool CDirectoryProvider::UpdateSort()
{
  // Evaluated before m_section is taken - see the lock order rule at the top of this file.
  SortBy sortMethod(SortUtils::SortMethodFromString(m_sortMethod.GetLabel(GetParentId(), false)));
  SortOrder sortOrder(SortUtils::SortOrderFromString(m_sortOrder.GetLabel(GetParentId(), false)));
  if (sortOrder == SortOrder::NONE)
    sortOrder = SortOrder::ASCENDING;

  std::unique_lock lock(m_section);
  if (sortMethod == m_currentSort.sortBy && sortOrder == m_currentSort.sortOrder)
    return false;

  m_currentSort.sortBy = sortMethod;
  m_currentSort.sortOrder = sortOrder;
  m_currentSort.sortAttributes = SortAttributeIgnoreFolders;

  if (CServiceBroker::GetSettingsComponent()->GetSettings()->GetBool(
          CSettings::SETTING_FILELISTS_IGNORETHEWHENSORTING))
    m_currentSort.sortAttributes =
        static_cast<SortAttribute>(m_currentSort.sortAttributes | SortAttributeIgnoreArticle);

  return true;
}
