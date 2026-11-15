import {
  Anywhere,
  AtLeast,
  CenterView,
  CreateUnit,
  DisplayText,
  ElapsedTime,
  MoveLocation,
  P1,
  UnitType,
  buildModule,
  once,
} from "../../../ts/index.ts";

// Diagnostic: does a DisplayText after CreateUnit/MoveLocation/CenterView get
// deferred behind a later trigger's DisplayText?
//
// A is the stock setup shape. B and C are later timed markers.
// D is CreateUnit without MoveLocation/CenterView. E is a later timed marker.
// If A appears above B above C, the setup marker is not deferred.
// If B appears above A, the setup marker was deferred by a slow action.
const STOCK_POINT = 63;

export const module = buildModule(() => {
  once(ElapsedTime(AtLeast, 1), () => {
    CreateUnit(1, UnitType.TerranMarine, Anywhere, P1);
    MoveLocation(STOCK_POINT, UnitType.TerranMarine, P1, Anywhere);
    CenterView(STOCK_POINT);
    DisplayText("A SETUP");
  });
  once(ElapsedTime(AtLeast, 2), () => {
    DisplayText("B TWO");
  });
  once(ElapsedTime(AtLeast, 3), () => {
    DisplayText("C THREE");
  });
  once(ElapsedTime(AtLeast, 4), () => {
    CreateUnit(1, UnitType.TerranMarine, Anywhere, P1);
    DisplayText("D CREATE ONLY");
  });
  once(ElapsedTime(AtLeast, 5), () => {
    DisplayText("E FIVE");
  });
});
