//! Module UPRP allocation for `CreateUnitWithProperties` (action 11).
//!
//! Port of eudplib's `init_property_map` / `GetPropertyIndex` /
//! `apply_property_map` split:
//!
//! * authored basemap slots (`UPUS[i] != 0`) are kept and normalized with
//!   `property_key`; duplicate authored records resolve to the last slot,
//!   matching eudplib's dictionary overwrite order;
//! * module records dedupe against authored and previously allocated
//!   records;
//! * a new record takes the lowest free slot (UPUS byte `0`), 1-based
//!   index written into the action's `value` (`+20`), and that
//!   slot's UPUS byte is enabled;
//! * all 64 slots occupied is a loud build error.
//!
//! Allocation is separate from CHK mutation: [`allocate`] patches the LIR
//! action indices and returns the UPRP/UPUS bytes to install. `assemble_map`
//! installs them just before `apply_property_map`, after the template
//! string table has been parsed (UPRP does not affect STR, but the tail
//! keeps its pristine-template ordering discipline).

use eudkit_lir::{LirDocument, LirItem};

use crate::chk::Chk;
use crate::fixmapdata::{property_key, PropertyTable};
use crate::layout::{UPRP_TABLE_SIZE, UPUS_COUNT};
use crate::MapTailError;

/// Replacement UPRP/UPUS bytes to install before `apply_property_map`.
#[derive(Debug)]
pub(crate) struct UprpUpdate {
    pub uprp: Vec<u8>,
    pub upus: Vec<u8>,
}

/// Allocate UPRP slots for every LIR action carrying a property record.
///
/// Returns `None` when the program has no module property records, leaving
/// the template's UPRP/UPUS untouched (including a missing UPUS).
pub(crate) fn allocate(
    doc: &mut LirDocument,
    chkt: &Chk,
) -> Result<Option<UprpUpdate>, MapTailError> {
    if !doc.module.items.iter().any(item_has_uprp) {
        return Ok(None);
    }

    let uprp = chkt
        .getsection(b"UPRP")
        .ok_or(MapTailError::MissingSection("UPRP"))?;
    if uprp.len() < UPRP_TABLE_SIZE {
        return Err(MapTailError::TruncatedSection("UPRP"));
    }
    // `init_property_map` treats a missing UPUS as all slots enabled.
    let mut upus = match chkt.getsection(b"UPUS") {
        Some(bytes) => {
            if bytes.len() < UPUS_COUNT {
                return Err(MapTailError::TruncatedSection("UPUS"));
            }
            bytes[..UPUS_COUNT].to_vec()
        }
        None => vec![1u8; UPUS_COUNT],
    };

    let mut table = PropertyTable::parse(uprp, &upus);

    for item in &mut doc.module.items {
        let LirItem::Trigger(trigger) = item else {
            continue;
        };
        for act in &mut trigger.acts {
            let Some(raw) = act.uprp else {
                continue;
            };
            if act.act.acttype != 11 {
                return Err(MapTailError::BadUprp(format!(
                    "UPRP record attached to action type {} (expected 11)",
                    act.act.acttype
                )));
            }
            let record = property_key(&raw);
            let slot = match table.slot_of(&record) {
                Some(slot) => slot,
                None => {
                    let Some(slot) = table.insert(record) else {
                        return Err(MapTailError::UprpOverflow);
                    };
                    upus[slot] = 1;
                    slot
                }
            };
            // SC's CreateUnitWithProperties counts UPRP slots from 1.
            act.set_uprp_slot(slot + 1);
        }
    }

    Ok(Some(UprpUpdate {
        uprp: table.to_bytes(),
        upus,
    }))
}

fn item_has_uprp(item: &LirItem) -> bool {
    match item {
        LirItem::Trigger(t) => t.acts.iter().any(|a| a.uprp.is_some()),
        LirItem::Blob(_) => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::layout::UPRP_RECORD_SIZE;
    use eudkit_lir::{CTrigAction, LirModule, TriggerItem};

    fn raw_record(hp: u8) -> [u8; 20] {
        let mut r = [0u8; 20];
        r[2..4].copy_from_slice(&(1u16 << 1).to_le_bytes());
        r[5] = hp;
        r
    }

    fn property_action(record: [u8; 20]) -> ActDataAlias {
        let mut act = CTrigAction {
            mask: 64,
            unit: 65,
            acttype: 11,
            modifier: 1,
            flags: 28,
            ..CTrigAction::empty()
        }
        .to_act_data();
        act.uprp = Some(record);
        act
    }

    type ActDataAlias = eudkit_lir::ActData;

    fn doc_with(acts: Vec<ActDataAlias>) -> LirDocument {
        LirDocument {
            version: eudkit_lir::LIR_VERSION.to_string(),
            module: LirModule {
                entry: 0,
                items: vec![LirItem::Trigger(TriggerItem {
                    size: 2408,
                    prev: None,
                    next: None,
                    prev_reloc: false,
                    next_reloc: false,
                    conds: Vec::new(),
                    acts,
                    preserved: true,
                    flag: None,
                })],
                strings: Vec::new(),
                str_addrs: Vec::new(),
            },
        }
    }

    fn chk(uprp: Vec<u8>, upus: Option<Vec<u8>>) -> Chk {
        let mut chkt = Chk::new();
        chkt.setsection(*b"UPRP", uprp);
        if let Some(upus) = upus {
            chkt.setsection(*b"UPUS", upus);
        }
        chkt
    }

    fn params(doc: &LirDocument) -> Vec<u32> {
        match &doc.module.items[0] {
            LirItem::Trigger(t) => t.acts.iter().map(|a| a.act.value).collect(),
            LirItem::Blob(_) => panic!("expected trigger"),
        }
    }

    #[test]
    fn allocates_first_free_slot_and_patches_action() {
        let rec = property_key(&raw_record(50));
        let mut doc = doc_with(vec![property_action(raw_record(50))]);
        let update = allocate(
            &mut doc,
            &chk(vec![0u8; UPRP_TABLE_SIZE], Some(vec![0u8; UPUS_COUNT])),
        )
        .unwrap()
        .expect("module record must allocate");

        assert_eq!(params(&doc), vec![1], "SC counts property slots from 1");
        assert_eq!(&update.uprp[..UPRP_RECORD_SIZE], &rec);
        assert!(update.uprp[UPRP_RECORD_SIZE..]
            .iter()
            .all(|&b| b == 0));
        assert_eq!(update.upus[0], 1);
        assert!(update.upus[1..].iter().all(|&b| b == 0));
    }

    #[test]
    fn dedupes_identical_records_and_allocates_distinct_ones() {
        let mut doc = doc_with(vec![
            property_action(raw_record(50)),
            property_action(raw_record(50)),
            property_action(raw_record(70)),
        ]);
        let update = allocate(
            &mut doc,
            &chk(vec![0u8; UPRP_TABLE_SIZE], Some(vec![0u8; UPUS_COUNT])),
        )
        .unwrap()
        .expect("module records must allocate");

        assert_eq!(params(&doc), vec![1, 1, 2]);
        assert_eq!(
            &update.uprp[..UPRP_RECORD_SIZE],
            &property_key(&raw_record(50))[..]
        );
        assert_eq!(
            &update.uprp[UPRP_RECORD_SIZE..2 * UPRP_RECORD_SIZE],
            &property_key(&raw_record(70))[..]
        );
        assert_eq!(&update.upus[..3], &[1, 1, 0]);
    }

    #[test]
    fn preserves_authored_slots_and_reuses_their_records() {
        let rec = property_key(&raw_record(50));
        let mut uprp = vec![0u8; UPRP_TABLE_SIZE];
        uprp[3 * UPRP_RECORD_SIZE..4 * UPRP_RECORD_SIZE].copy_from_slice(&raw_record(50));
        uprp[5 * UPRP_RECORD_SIZE..6 * UPRP_RECORD_SIZE].copy_from_slice(&raw_record(50));
        let mut upus = vec![0u8; UPUS_COUNT];
        upus[3] = 1;
        upus[5] = 1;

        let mut doc = doc_with(vec![
            property_action(raw_record(50)),
            property_action(raw_record(70)),
        ]);
        let update = allocate(&mut doc, &chk(uprp, Some(upus)))
            .unwrap()
            .expect("module records must allocate");

        // The authored duplicate resolves to the last slot, matching
        // eudplib's dictionary-overwrite order; the new record takes slot 0.
        assert_eq!(params(&doc), vec![6, 1]);
        assert_eq!(
            &update.uprp[3 * UPRP_RECORD_SIZE..4 * UPRP_RECORD_SIZE],
            &rec[..]
        );
        assert_eq!(
            &update.uprp[5 * UPRP_RECORD_SIZE..6 * UPRP_RECORD_SIZE],
            &rec[..]
        );
        assert_eq!(
            &update.uprp[..UPRP_RECORD_SIZE],
            &property_key(&raw_record(70))[..]
        );
        assert_eq!(&update.upus[..6], &[1, 0, 0, 1, 0, 1]);
    }

    #[test]
    fn write_back_normalizes_the_same_table_allocation_returns() {
        // The authored duplicate must resolve to the last slot, and the
        // fixed-map-data write-back must emit exactly the table allocation
        // produced (normalized records, same slot positions).
        let mut uprp = vec![0u8; UPRP_TABLE_SIZE];
        uprp[3 * UPRP_RECORD_SIZE..4 * UPRP_RECORD_SIZE].copy_from_slice(&raw_record(50));
        uprp[5 * UPRP_RECORD_SIZE..6 * UPRP_RECORD_SIZE].copy_from_slice(&raw_record(50));
        let mut upus = vec![0u8; UPUS_COUNT];
        upus[3] = 1;
        upus[5] = 1;

        let mut doc = doc_with(vec![
            property_action(raw_record(50)),
            property_action(raw_record(70)),
        ]);
        let update = allocate(&mut doc, &chk(uprp, Some(upus)))
            .unwrap()
            .expect("module records must allocate");
        assert_eq!(
            params(&doc),
            vec![6, 1],
            "authored duplicate -> slot 5 (1-based 6)"
        );

        let mut installed = chk(update.uprp.clone(), Some(update.upus.clone()));
        crate::fixmapdata::apply_property_map(&mut installed).unwrap();
        assert_eq!(installed.getsection(b"UPRP").unwrap(), &update.uprp[..]);
    }

    #[test]
    fn all_zero_authored_record_dedupes_to_last_slot() {
        let mut doc = doc_with(vec![property_action([0u8; 20])]);
        let update = allocate(
            &mut doc,
            &chk(vec![0u8; UPRP_TABLE_SIZE], Some(vec![1u8; UPUS_COUNT])),
        )
        .unwrap()
        .expect("zero record must allocate");

        assert_eq!(params(&doc), vec![64]);
        assert_eq!(&update.uprp[63 * UPRP_RECORD_SIZE..], &[0u8; 20]);
        assert_eq!(update.upus, vec![1u8; UPUS_COUNT]);
    }

    #[test]
    fn exhaustion_is_a_clear_error() {
        let mut doc = doc_with(vec![property_action(raw_record(50))]);
        let err = allocate(
            &mut doc,
            &chk(vec![0u8; UPRP_TABLE_SIZE], Some(vec![1u8; UPUS_COUNT])),
        )
        .unwrap_err();
        assert!(matches!(err, MapTailError::UprpOverflow));
    }

    #[test]
    fn no_module_records_leave_the_template_untouched() {
        let mut doc = doc_with(vec![]);
        assert!(allocate(&mut doc, &Chk::new()).unwrap().is_none());
    }

    #[test]
    fn missing_uprp_is_loud_when_a_record_needs_allocation() {
        let mut doc = doc_with(vec![property_action(raw_record(50))]);
        let err = allocate(&mut doc, &Chk::new()).unwrap_err();
        assert!(matches!(err, MapTailError::MissingSection("UPRP")));
    }

    #[test]
    fn record_on_non_cuwp_action_is_loud() {
        let mut act = CTrigAction::empty().to_act_data();
        act.uprp = Some([0u8; 20]);
        let mut doc = doc_with(vec![act]);
        let err = allocate(
            &mut doc,
            &chk(vec![0u8; UPRP_TABLE_SIZE], Some(vec![0u8; UPUS_COUNT])),
        )
        .unwrap_err();
        assert!(matches!(err, MapTailError::BadUprp(_)));
    }
}
