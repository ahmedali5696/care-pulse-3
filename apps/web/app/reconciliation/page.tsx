"use client";

import { useEffect, useState } from "react";

type Entry = { appointment_id: string; patient_name: string; status: string; lane: string; booking_number: number; appointment_revision: number; claim_revision: number | null; overdue: boolean; requires_review: boolean };
type Day = { service_date: string; review_revision: number; review_disposition: string | null; review_notes: string | null; main_queue_day_revision: number; emergency_queue_day_revision: number; entries: Entry[]; total: number; offset: number; limit: number };
type Command = { path: string; body: string; key: string };

const copy = {
  en: { title: "Day reconciliation", language: "العربية", date: "Service date", load: "Load day", loading: "Loading…", review: "Review state", none: "Not reviewed", notes: "Review notes", reason: "Reason", save: "Save review", close: "Close day", list: "Appointments", empty: "No open appointments on this page.", overdue: "Overdue", needs: "Requires review", absence: "Mark verified absence", verify: "I verified that this patient was absent", submit: "Confirm no-show", cancel: "Cancel", prev: "Previous", next: "Next", error: "The request failed. Refresh the day and check your permissions or the current state.", uncertain: "The outcome is unknown. Retry the same request before making another change.", retry: "Retry request", done: "Saved. The day has been refreshed.", owner: "Only the owner can change an existing review.", waiting: "Close is rejected if anyone is waiting, including on another page.", status: "Status", lane: "Lane", page: "Showing" },
  ar: { title: "مراجعة اليوم", language: "English", date: "تاريخ الخدمة", load: "عرض اليوم", loading: "جارٍ التحميل…", review: "حالة المراجعة", none: "لم تتم المراجعة", notes: "ملاحظات المراجعة", reason: "السبب", save: "حفظ المراجعة", close: "إغلاق اليوم", list: "المواعيد", empty: "لا توجد مواعيد مفتوحة في هذه الصفحة.", overdue: "متأخر", needs: "يتطلب مراجعة", absence: "تسجيل غياب تم التحقق منه", verify: "تحققت من غياب هذا المريض", submit: "تأكيد عدم الحضور", cancel: "إلغاء", prev: "السابق", next: "التالي", error: "تعذر تنفيذ الطلب. أعد تحميل اليوم وتحقق من الصلاحيات والحالة الحالية.", uncertain: "النتيجة غير معروفة. أعد محاولة الطلب نفسه قبل إجراء تغيير آخر.", retry: "إعادة المحاولة", done: "تم الحفظ وتحديث اليوم.", owner: "يمكن للمالك فقط تعديل مراجعة موجودة.", waiting: "يُرفض إغلاق اليوم عند وجود مريض في الانتظار، بما في ذلك الصفحات الأخرى.", status: "الحالة", lane: "المسار", page: "المعروض" },
} as const;

const labels: Record<string, string> = { Booked: "محجوز", Waiting: "في الانتظار", "In Examination": "قيد الكشف", MAIN: "رئيسي", EMERGENCY: "طوارئ", REVIEWED: "تمت المراجعة", FINAL_CLOSED: "مغلق نهائيًا" };

export default function ReconciliationPage() {
  const [language, setLanguage] = useState<"en" | "ar">("en");
  const [date, setDate] = useState("");
  const [day, setDay] = useState<Day | null>(null);
  const [notes, setNotes] = useState("");
  const [reason, setReason] = useState("");
  const [selected, setSelected] = useState<Entry | null>(null);
  const [verified, setVerified] = useState(false);
  const [pending, setPending] = useState<Command | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<"error" | "uncertain" | "done" | null>(null);
  const t = copy[language];
  const label = (value: string) => language === "ar" ? labels[value] ?? value : value;

  useEffect(() => { if (navigator.language.toLowerCase().startsWith("ar")) setLanguage("ar"); }, []);

  async function loadDay(selectedDate: string, offset = 0) {
    setBusy(true);
    try {
      const response = await fetch(`/api/reconciliation?date=${encodeURIComponent(selectedDate)}&limit=50&offset=${offset}`, { cache: "no-store" });
      if (!response.ok) throw new Error("read failed");
      const payload = await response.json() as { data: Day };
      setDay(payload.data);
      setNotes(payload.data.review_notes ?? "");
      setSelected(null);
      setVerified(false);
      return true;
    } catch { setDay(null); setMessage("error"); return false; }
    finally { setBusy(false); }
  }

  async function send(command: Command) {
    setBusy(true);
    setPending(command);
    setMessage(null);
    try {
      const response = await fetch(command.path, { method: "POST", headers: { "Content-Type": "application/json", "X-Requested-With": "XMLHttpRequest", "Idempotency-Key": command.key }, body: command.body });
      if (response.status >= 500) { setMessage("uncertain"); return; }
      setPending(null);
      if (!response.ok) { setMessage("error"); await loadDay(day!.service_date, day!.offset); return; }
      setReason("");
      if (await loadDay(day!.service_date, day!.offset)) setMessage("done");
    } catch { setMessage("uncertain"); }
    finally { setBusy(false); }
  }

  function submit(path: string, body: object) {
    void send({ path, body: JSON.stringify(body), key: crypto.randomUUID() });
  }

  function review(close: boolean) {
    if (!day) return;
    const body = { expected_review_revision: day.review_revision, notes: notes || null, reason: reason || null };
    submit(`/api/days/${day.service_date}/${close ? "close" : "review"}`, close ? { ...body, expected_main_queue_day_revision: day.main_queue_day_revision, expected_emergency_queue_day_revision: day.emergency_queue_day_revision } : body);
  }

  return <main lang={language} dir={language === "ar" ? "rtl" : "ltr"} style={{ maxWidth: 900, margin: "2rem auto", padding: "0 1rem", fontFamily: "sans-serif", lineHeight: 1.6 }}>
    <button type="button" onClick={() => setLanguage(language === "en" ? "ar" : "en")}>{t.language}</button>
    <h1>{t.title}</h1>
    <form onSubmit={(event) => { event.preventDefault(); setMessage(null); void loadDay(date); }}>
      <label>{t.date} <input type="date" required disabled={busy || !!pending} value={date} onChange={(event) => setDate(event.target.value)} /></label>{" "}
      <button disabled={busy || !!pending}>{busy ? t.loading : t.load}</button>
    </form>
    {message && <p role={message === "done" ? "status" : "alert"}>{t[message]}</p>}
    {pending && <button disabled={busy} onClick={() => void send(pending)}>{t.retry}</button>}
    {day && <>
      <h2>{day.service_date} — {t.review}</h2>
      <p>{day.review_disposition ? label(day.review_disposition) : t.none} (v{day.review_revision})</p>
      <p>{t.owner} {t.waiting}</p>
      <label>{t.notes}<br /><textarea value={notes} onChange={(event) => setNotes(event.target.value)} /></label><br />
      <label>{t.reason}<br /><input value={reason} onChange={(event) => setReason(event.target.value)} maxLength={512} /></label><br />
      <button disabled={busy || !!pending || !!selected || (day.review_revision > 0 && !reason.trim())} onClick={() => review(false)}>{t.save}</button>{" "}
      <button disabled={busy || !!pending || !!selected || (day.review_revision > 0 && !reason.trim())} onClick={() => review(true)}>{t.close}</button>
      <h2>{t.list}</h2>
      {day.entries.length === 0 ? <p>{t.empty}</p> : <ul>{day.entries.map((entry) => <li key={entry.appointment_id}>
        <strong dir="auto">{entry.patient_name}</strong> — #{entry.booking_number} — {t.status}: {label(entry.status)} — {t.lane}: {label(entry.lane)}
        {entry.overdue && <> — {t.overdue}</>}{entry.requires_review && <> — {t.needs}</>}{" "}
        {entry.overdue && entry.status === "Booked" && <button disabled={busy || !!pending} onClick={() => { setSelected(entry); setVerified(false); setReason(""); }}>{t.absence}</button>}
      </li>)}</ul>}
      <p>{t.page} {day.offset + (day.entries.length ? 1 : 0)}–{day.offset + day.entries.length} / {day.total}</p>
      <button disabled={busy || !!pending || day.offset === 0} onClick={() => void loadDay(day.service_date, Math.max(0, day.offset - day.limit))}>{t.prev}</button>{" "}
      <button disabled={busy || !!pending || day.offset + day.limit >= day.total} onClick={() => void loadDay(day.service_date, day.offset + day.limit)}>{t.next}</button>
      {selected && <form onSubmit={(event) => { event.preventDefault(); if (verified && reason.trim()) submit(`/api/appointments/${selected.appointment_id}/manual-no-show`, { verified_absence: true, reason, expected_appointment_revision: selected.appointment_revision, expected_claim_revision: selected.claim_revision }); }}>
        <h2>{t.absence}: <span dir="auto">{selected.patient_name}</span></h2>
        <label><input type="checkbox" checked={verified} onChange={(event) => setVerified(event.target.checked)} required /> {t.verify}</label><br />
        <label>{t.reason} <input required maxLength={512} value={reason} onChange={(event) => setReason(event.target.value)} /></label><br />
        <button disabled={busy || !!pending || !verified || !reason.trim()}>{t.submit}</button>{" "}
        <button type="button" disabled={busy || !!pending} onClick={() => setSelected(null)}>{t.cancel}</button>
      </form>}
    </>}
  </main>;
}
